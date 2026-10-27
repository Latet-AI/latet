"""The package works without the LangChain extra: ``import latet_client`` does not touch ``langchain_core``, and
``latet_client.langchain`` says how to install it."""

import subprocess
import sys

PROGRAM = """
import sys
sys.modules["langchain_core"] = None  # as if it were not installed: any import of it fails
import latet_client
assert latet_client.Latet and latet_client.AsyncLatet and latet_client.__version__
assert "latet_client.langchain" not in sys.modules
try:
    import latet_client.langchain
except ImportError as error:
    print(error)
else:
    raise SystemExit("latet_client.langchain imported without langchain_core")
"""


def test_the_package_imports_without_langchain_and_the_extra_says_how_to_install_it():
    ran = subprocess.run([sys.executable, "-I", "-c", PROGRAM], capture_output=True, text=True, timeout=60, check=False)
    assert ran.returncode == 0, ran.stderr
    assert 'pip install "latet-client[langchain]"' in ran.stdout
