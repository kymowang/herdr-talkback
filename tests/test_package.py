import re

import herdr_talkback


def test_package_exports_version():
    assert re.fullmatch(r"\d+\.\d+\.\d+", herdr_talkback.__version__)
    assert herdr_talkback.__version__ == "0.2.0"
