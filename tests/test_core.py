"""Unit tests for diff parsing, config filtering, and JSON extraction."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pikuscope.config import Config
from pikuscope.diff import parse_unified_diff
from pikuscope.llm import extract_json

SAMPLE = """diff --git a/src/app.ts b/src/app.ts
index 1111111..2222222 100644
--- a/src/app.ts
+++ b/src/app.ts
@@ -1,4 +1,5 @@
 import { x } from "./x";
-const a = 1;
+const a = 2;
+const b = 3;
 export function main() {
@@ -20,3 +21,3 @@ export function main() {
 }
-const z = a;
+const z = b;
 export default main;
diff --git a/new.py b/new.py
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/new.py
@@ -0,0 +1,2 @@
+def f():
+    return 1
diff --git a/gone.txt b/gone.txt
deleted file mode 100644
index 4444444..0000000
--- a/gone.txt
+++ /dev/null
@@ -1,1 +0,0 @@
-bye
"""


def test_parse_files():
    fds = parse_unified_diff(SAMPLE)
    assert [f.path for f in fds] == ["src/app.ts", "new.py", "gone.txt"]
    assert fds[0].status == "modified"
    assert fds[1].status == "added"
    assert fds[2].status == "removed"


def test_line_numbers():
    fds = parse_unified_diff(SAMPLE)
    app = fds[0]
    assert app.added_line_numbers() == {2, 3, 22}
    assert 1 in app.new_line_numbers()  # context line
    assert app.additions == 3
    assert app.deletions == 2
    newpy = fds[1]
    assert newpy.added_line_numbers() == {1, 2}


def test_annotated_contains_numbers():
    fds = parse_unified_diff(SAMPLE)
    ann = fds[0].annotated()
    assert "    2 + const a = 2;" in ann
    assert "    2 - const a = 1;" in ann  # old-side number for deletion


def test_config_filters():
    cfg = Config()
    assert not cfg.is_reviewable("pnpm-lock.yaml")
    assert not cfg.is_reviewable("apps/web/package-lock.json")
    assert not cfg.is_reviewable("x/dist/bundle.js")
    assert cfg.is_reviewable("apps/web/src/App.tsx")
    cfg2 = Config.load(data={"reviews": {"path_filters": ["**/fixtures/**", "!important/fixtures/keep.ts"]}})
    assert not cfg2.is_reviewable("a/fixtures/x.ts")
    assert cfg2.is_reviewable("important/fixtures/keep.ts")


def test_path_instructions():
    cfg = Config.load(data={"reviews": {"path_instructions": [
        {"path": "apps/mobile/**", "instructions": "check platforms"}]}})
    assert cfg.instructions_for("apps/mobile/src/a.ts") == ["check platforms"]
    assert cfg.instructions_for("apps/web/src/a.ts") == []


def test_extract_json():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('prose\n```json\n{"a": 1}\n```\nmore') == {"a": 1}
    assert extract_json('Here it is: {"findings": [{"x": "y\\"z"}]} done')["findings"][0]["x"] == 'y"z'
    assert extract_json("[1, 2]") == [1, 2]
