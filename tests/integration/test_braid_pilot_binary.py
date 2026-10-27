"""Optional actual stdio binary contract. No model, sockets or live data."""
import os
from pathlib import Path

import pytest

from muninn.index.braid_client import BraidClient


def test_pilot_real_binary_negotiates_required_contract(tmp_path):
    raw = os.environ.get("MUNINN_TEST_BRAID_BINARY")
    if not raw or not Path(raw).is_file():
        pytest.skip("set MUNINN_TEST_BRAID_BINARY to an approved pinned Braid executable")
    client = BraidClient.create(binary=Path(raw), generation_dir=tmp_path / "synthetic",
                                dataset_id="synthetic:pilot-contract", profile_id="synthetic-space")
    try:
        assert "query_embedding" in client.capabilities
        assert client.request("dataset")["id"] == "synthetic:pilot-contract"
    finally:
        client.close()
