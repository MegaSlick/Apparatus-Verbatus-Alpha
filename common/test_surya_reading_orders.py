"""The page path's Surya reading orders are the runner's own words.

`operations/serving/surya/contract.py` runs in Surya's environment, which cannot
import `common/`, and `common/` never imports `operations/`; so the page path
restates the two words and this test holds them equal to the runner's.
"""

from common import page_path
from operations.serving.surya_detector import contract


def test_the_page_path_states_surya_reading_orders_as_the_runner_writes_them():
    assert page_path.SURYA_ORDER_HEAD == contract.ORDER_HEAD
    assert page_path.SURYA_RASTER_FALLBACK == contract.RASTER_FALLBACK
    assert page_path.SURYA_READING_ORDERS == contract.READING_ORDERS
