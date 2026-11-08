"""scout() on a fake Home Assistant config folder. Run: python coxd/test_scout_ha.py"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import registry  # noqa: E402

d = Path(tempfile.mkdtemp(prefix="fake-ha-"))
(d / "configuration.yaml").write_text("homeassistant:\n  name: Fake Home\n")
(d / "deploy.sh").write_text("#!/bin/sh\n")  # must NOT enable a deploy command
e = registry.scout(d)
ok = (e["lint"] == "yamllint ." and e["test"] == "yamllint ."
      and e["source"] == "configuration.yaml"
      and e["deploy"]["enabled"] is False and e["deploy"]["command"] is None
      and "secrets.yaml" in e["sacred_globs"] and ".storage/**" in e["sacred_globs"]
      and ".env" in e["sacred_globs"])
print("ok  ha scout" if ok else f"FAIL ha scout {e}")
sys.exit(0 if ok else 1)
