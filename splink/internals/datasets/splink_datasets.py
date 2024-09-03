from __future__ import annotations

import io
from pathlib import Path
from urllib.request import urlopen

import pandas as pd

from .metadata import datasets

_DATASETDIR = Path(__file__).parent

_cache_dir = _DATASETDIR / "__splinkdata_cache__"


def datafile_exists(file_loc):
    return file_loc.is_file()


def dataset_property(att):
    ds_meta = att()
    dataset_name = ds_meta.dataset_name
    url = ds_meta.url
    data_format = ds_meta.data_format

    def lazyload_data(self) -> pd.DataFrame:
        file_loc = _cache_dir / f"{dataset_name}.{data_format}"
        if not datafile_exists(file_loc):
            print(f"downloading: {url}")  # noqa: T201
            data = urlopen(url)
            print("")  # noqa: T201

            data_stream = io.BytesIO(data.read())
            _cache_dir.mkdir(exist_ok=True)
            with open(file_loc, "bw+") as write_file:
                write_file.write(data_stream.getvalue())
            data_stream.seek(0)
        else:
            data_stream = file_loc

        read_function = {
            "csv": pd.read_csv,
            "parquet": pd.read_parquet,
        }.get(data_format, None)

        # just in case we have an invalid format
        if read_function is None:
            raise ValueError(
                f"Error retrieving dataset {dataset_name} - invalid format!"
            )
        return read_function(data_stream)

    return lazyload_data

class SplinkDataSets:
    @property
    @dataset_property
    def fake_1000():
        """
        Fake 1000 from splink demos.
        Records are 250 simulated people, with different numbers of duplicates, labelled.

        Columns:
        unique_id, first_name, surname, dob, city, email, cluster
        """  # NOQA: E501
        return datasets["fake_1000"]

    @property
    @dataset_property
    def historical_50k():
        """
        The data is based on historical persons scraped from wikidata.
        Duplicate records are introduced with a variety of errors.

        Columns:
        unique_id, cluster, full_name, first_and_surname, first_name, surname, dob, birth_place, postcode_fake, gender, occupation
        """  # NOQA: E501
        return datasets["historical_50k"]


# these two singleton objects are the only user-facing portion:
splink_datasets = SplinkDataSets()
