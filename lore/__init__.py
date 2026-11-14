__version__ = "0.1.0"

import warnings as _warnings

# pysbd (sentence splitting) has invalid escape sequences that Python reports
# as SyntaxWarnings the first time it compiles the package: harmless, and
# alarming in a first run's output.
_warnings.filterwarnings("ignore", category=SyntaxWarning, module=r".*pysbd.*")
