from pathlib import Path

import pytest

from cr_films_to_prehrajto.models import Film


@pytest.fixture
def film():
    return Film(
        cr_film_id=42,
        slug="pelisky",
        title="Pelíšky",
        original_title="Cosy Dens",
        year=1999,
        runtime_min=115,
        original_language="cs",
        description="Description",
        tmdb_id=123,
        imdb_id="tt0167331",
    )


@pytest.fixture
def fixtures():
    return Path(__file__).parent / "fixtures"
