from datetime import datetime
import logging
from typing import Dict, Collection, Union, Optional, List

import json
from abc import ABC, abstractmethod
from pathlib import Path
import tqdm
import itertools

import requests

import sec_certs.helpers as helpers
import sec_certs.constants as constants
import sec_certs.parallel_processing as cert_processing

from sec_certs.sample.certificate import Certificate
from sec_certs.serialization import CustomJSONDecoder, CustomJSONEncoder, ComplexSerializableType
from sec_certs.config.configuration import config
from sec_certs.serialization import serialize
from sec_certs.dataset.cpe import CPEDataset
from sec_certs.dataset.cve import CVEDataset
from sec_certs.model.cpe_matching import CPEClassifier

logger = logging.getLogger(__name__)


class Dataset(ABC, ComplexSerializableType):
    def __init__(self, certs: Dict[str, 'Certificate'], root_dir: Path, name: str = 'dataset name',
                 description: str = 'dataset_description'):
        self._root_dir = root_dir
        self.timestamp = datetime.now()
        self.sha256_digest = 'not implemented'
        self.name = name
        self.description = description
        self.certs = certs

    @property
    def root_dir(self):
        return self._root_dir

    @root_dir.setter
    def root_dir(self, new_dir: Union[str, Path]):
        new_dir = Path(new_dir)
        new_dir.mkdir(exist_ok=True)
        self._root_dir = new_dir

    @property
    def web_dir(self) -> Path:
        return self.root_dir / 'web'

    @property
    def auxillary_datasets_dir(self) -> Path:
        return self.root_dir / 'auxillary_datasets'

    @property
    def cpe_dataset_path(self) -> Path:
        return self.auxillary_datasets_dir / 'cpe_dataset.json'

    @property
    def cve_dataset_path(self) -> Path:
        return self.auxillary_datasets_dir / 'cve_dataset.json'

    @property
    def json_path(self) -> Path:
        return self.root_dir / (self.name + '.json')

    def __contains__(self, item):
        if not issubclass(type(item), Certificate):
            return False
        return item.dgst in self.certs

    def __iter__(self):
        yield from self.certs.values()

    def __getitem__(self, item: str):
        return self.certs.__getitem__(item.lower())

    def __setitem__(self, key: str, value: 'Certificate'):
        self.certs.__setitem__(key.lower(), value)

    def __len__(self) -> int:
        return len(self.certs)

    def __eq__(self, other: 'Dataset') -> bool:
        return self.certs == other.certs

    def __str__(self) -> str:
        return str(type(self).__name__) + ':' + self.name + ', ' + str(len(self)) + ' certificates'

    def to_dict(self):
        return {'timestamp': self.timestamp, 'sha256_digest': self.sha256_digest,
                'name': self.name, 'description': self.description,
                'n_certs': len(self), 'certs': list(self.certs.values())}

    @classmethod
    def from_dict(cls, dct: Dict):
        certs = {x.dgst: x for x in dct['certs']}
        dset = cls(certs, Path('../'), dct['name'], dct['description'])
        if len(dset) != (claimed := dct['n_certs']):
            logger.error(
                f'The actual number of certs in dataset ({len(dset)}) does not match the claimed number ({claimed}).')
        return dset

    @classmethod
    def from_json(cls, input_path: Union[str, Path]):
        dset = ComplexSerializableType.from_json(input_path)
        dset.root_dir = Path(input_path).parent.absolute()
        dset.set_local_paths()
        return dset

    def set_local_paths(self):
        raise NotImplementedError('Not meant to be implemented by the base class.')

    @abstractmethod
    def get_certs_from_web(self):
        raise NotImplementedError('Not meant to be implemented by the base class.')

    @abstractmethod
    def convert_all_pdfs(self):
        raise NotImplementedError('Not meant to be implemented by the base class.')

    @abstractmethod
    def download_all_pdfs(self):
        raise NotImplementedError('Not meant to be implemented by the base class.')

    @staticmethod
    def _download_parallel(urls: Collection[str], paths: Collection[Path], prune_corrupted: bool = True):
        exit_codes = cert_processing.process_parallel(helpers.download_file,
                                                      list(zip(urls, paths)),
                                                      config.n_threads,
                                                      unpack=True)
        n_successful = len([e for e in exit_codes if e == requests.codes.ok])
        logger.info(f'Successfully downloaded {n_successful} files, {len(exit_codes) - n_successful} failed.')

        for url, e in zip(urls, exit_codes):
            if e != requests.codes.ok:
                logger.error(f'Failed to download {url}, exit code: {e}')

        if prune_corrupted is True:
            for p in paths:
                if p.exists() and p.stat().st_size < constants.MIN_CORRECT_CERT_SIZE:
                    logger.error(f'Corrupted file at: {p}')
                    p.unlink()

    def _prepare_cpe_dataset(self, download_fresh_cpes: bool = False):
        logger.info('Preparing CPE dataset.')
        if not self.auxillary_datasets_dir.exists():
            self.auxillary_datasets_dir.mkdir(parents=True)

        if not self.cpe_dataset_path.exists() or download_fresh_cpes is True:
            cpe_dataset = CPEDataset.from_web(self.cpe_dataset_path)
            cpe_dataset.to_json(str(self.cpe_dataset_path))
        else:
            cpe_dataset = CPEDataset.from_json(str(self.cpe_dataset_path))

        return cpe_dataset

    def _prepare_cve_dataset(self, download_fresh_cves: bool = False) -> CVEDataset:
        logger.info('Preparing CVE dataset.')
        if not self.auxillary_datasets_dir.exists():
            self.auxillary_datasets_dir.mkdir(parents=True)

        if not self.cve_dataset_path.exists() or download_fresh_cves is True:
            cve_dataset = CVEDataset.from_web()
            cve_dataset.to_json(str(self.cve_dataset_path))
        else:
            cve_dataset = CVEDataset.from_json(str(self.cve_dataset_path))

        cve_dataset.build_lookup_dict()
        return cve_dataset

    def _compute_candidate_versions(self):
        logger.info('Computing heuristics: possible product versions in sample name')
        for cert in self:
            cert.compute_heuristics_version()

    def _compute_cpe_matches(self, download_fresh_cpes: bool = False) -> CPEClassifier:
        logger.info('Computing heuristics: Finding CPE matches for certificates')
        cpe_dset = self._prepare_cpe_dataset(download_fresh_cpes)
        if not cpe_dset.was_enhanced_with_vuln_cpes:
            cve_dset = self._prepare_cve_dataset(False)
            cpe_dset.enhance_with_cpes_from_cve_dataset(cve_dset)

        clf = CPEClassifier(config.cpe_matching_threshold, config.cpe_n_max_matches)
        clf.fit([x for x in cpe_dset])

        for cert in tqdm.tqdm(self, desc='Predicting CPE matches with the classifier'):
            cert.compute_heuristics_cpe_match(clf)

        return clf

    @serialize
    def compute_cpe_heuristics(self) -> CPEClassifier:
        self._compute_candidate_versions()
        return self._compute_cpe_matches()

    def to_label_studio_json(self, output_path: Union[str, Path]):
        lst = []
        for cert in [x for x in self if x.heuristics.cpe_matches]:
            dct = {'text': cert.label_studio_title}
            candidates = [x[1].title for x in cert.heuristics.cpe_matches]
            candidates += ['No good match'] * (config.cc_cpe_max_matches - len(candidates))
            options = ['option_' + str(x) for x in range(1, 21)]
            dct.update({o: c for o, c in zip(options, candidates)})
            lst.append(dct)

        with Path(output_path).open('w') as handle:
            json.dump(lst, handle, indent=4)

    @serialize
    def load_label_studio_labels(self, input_path: Union[str, Path]):
        with Path(input_path).open('r') as handle:
            data = json.load(handle)

        cpe_dset = self._prepare_cpe_dataset()

        logger.info('Translating label studio matches into their CPE representations and assigning to certificates.')
        for annotation in tqdm.tqdm([x for x in data if 'verified_cpe_match' in x], desc='Translating label studio matches'):
            match_keys = annotation['verified_cpe_match']
            match_keys = [match_keys] if isinstance(match_keys, str) else match_keys['choices']
            match_keys = [x.lstrip('$') for x in match_keys]
            predicted_annotations = [annotation[x] for x in match_keys if annotation[x] != 'No good match']

            cpes = set()
            for x in predicted_annotations:
                if x not in cpe_dset.title_to_cpes:
                    print(f'Error: {x} not in dataset')
                else:
                    to_update = cpe_dset.title_to_cpes[x]
                    if to_update and not cpes:
                        cpes = to_update
                    elif to_update and cpes:
                        cpes = cpes.update(to_update)


            # cpes = set(itertools.chain.from_iterable([cpe_dset.title_to_cpes.get(x, []) for x in predicted_annotations]))

            # distinguish between FIPS and CC
            if '\n' in annotation['text']:
                cert_name = annotation['text'].split('\nModule name: ')[1].split('\n')[0]
            else:
                cert_name = annotation['text']

            certs = self.get_certs_from_name(cert_name)

            for c in certs:
                c.heuristics.verified_cpe_matches = {x.uri for x in cpes} if cpes else None

    def get_certs_from_name(self, name: str) -> List[Certificate]:
        raise NotImplementedError('Not meant to be implemented by the base class.')

    def enrich_automated_cpes_with_manual_labels(self):
        """
        Prior to CVE matching, it is wise to expand the database of automatic CPE matches with those that were manually assigned.
        """
        for cert in self:
            if not cert.heuristics.cpe_matches and cert.heuristics.verified_cpe_matches:
                cert.heuristics.cpe_matches = cert.heuristics.verified_cpe_matches
            elif cert.heuristics.cpe_matches and cert.heuristics.verified_cpe_matches:
                cert.heuristics.cpe_matches = cert.heuristics.cpe_matches.union(cert.heuristics.verified_cpe_matches)

    @serialize
    def compute_related_cves(self, download_fresh_cves: bool = False):
        logger.info('Retrieving related CVEs to verified CPE matches')
        cve_dset = self._prepare_cve_dataset(download_fresh_cves)

        self.enrich_automated_cpes_with_manual_labels()
        cpe_rich_certs = [x for x in self if x.heuristics.cpe_matches]

        relevant_cpes = set(itertools.chain.from_iterable([x.heuristics.cpe_matches for x in cpe_rich_certs]))
        cve_dset.filter_related_cpes(relevant_cpes)

        for cert in tqdm.tqdm(cpe_rich_certs, desc='Computing related CVES'):
            cert.compute_heuristics_related_cves(cve_dset)

        n_vulnerable = len([x for x in cpe_rich_certs if x.heuristics.related_cves])
        n_vulnerabilities = sum(
            [len(x.heuristics.related_cves) for x in cpe_rich_certs if x.heuristics.related_cves])
        logger.info(
            f'In total, we identified {n_vulnerabilities} vulnerabilities in {n_vulnerable} vulnerable certificates.')
