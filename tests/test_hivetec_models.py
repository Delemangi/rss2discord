import pytest
from pydantic import ValidationError

from rss2discord.transports.hivetec_models import HivetecProduct
from tests.hivetec_helpers import product_payload


@pytest.mark.parametrize("name", ["   ", "\x00\n\t"])
def test_hivetec_product_rejects_names_empty_after_normalization(name: str) -> None:
    payload = product_payload(1)
    payload["name"] = name

    with pytest.raises(ValidationError):
        HivetecProduct.model_validate(payload)


@pytest.mark.parametrize("name", ["   ", "\x00\n\t"])
def test_hivetec_product_rejects_category_names_empty_after_normalization(
    name: str,
) -> None:
    payload = product_payload(1)
    payload["categories"] = [{"name": name}]

    with pytest.raises(ValidationError):
        HivetecProduct.model_validate(payload)
