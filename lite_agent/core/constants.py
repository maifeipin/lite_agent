import os

# PACKAGE_ROOT is the root of the lite_agent package (/.../lite_agent)
PACKAGE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# PROJECT_ROOT is the root of the workspace repository (/.../lite_agent/..)
PROJECT_ROOT = os.path.dirname(PACKAGE_ROOT)
