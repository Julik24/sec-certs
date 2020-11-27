from sec_certs.dataset import CCDataset
from sec_certs.serialization import CustomJSONEncoder, CustomJSONDecoder
import sec_certs.constants as constants
from pathlib import Path
from datetime import datetime
import logging
import json

logger = logging.getLogger(__name__)


def main():
    file_handler = logging.FileHandler(constants.LOGS_FILENAME)
    stream_handler = logging.StreamHandler()
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    file_handler.setFormatter(formatter)
    stream_handler.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=[file_handler, stream_handler])
    start = datetime.now()

    # Create empty dataset
    dset = CCDataset({}, Path('./debug_dataset'), 'sample_dataset', 'sample dataset description')

    # Load metadata for certificates from CSV and HTML sources
    dset.get_certs_from_web()
    logger.info(f'Finished parsing. Have dataset with {len(dset)} certificates.')

    # Dump dataset into JSON
    with open('./debug_dataset/cc_full_dataset.json', 'w') as handle:
        json.dump(dset, handle, cls=CustomJSONEncoder, indent=4)

    # Load dataset from JSON
    with open('./debug_dataset/cc_full_dataset.json', 'r') as handle:
        new_dset = json.load(handle, cls=CustomJSONDecoder)
    new_dset.root_dir = Path('/Users/adam/phd/projects/certificates/sec-certs/debug_dataset')

    assert dset == new_dset

    # Download pdfs
    dset.download_all_pdfs()

    # Convert pdfs to text
    new_dset.convert_all_pdfs()

    end = datetime.now()
    logger.info(f'The computation took {(end-start)} seconds.')


if __name__ == '__main__':
    main()
