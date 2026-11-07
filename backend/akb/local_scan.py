"""Resumable, finite local scanning worker. Never logs paths, text or credentials."""

import os
import sys

from akb.local_index import LocalIndex


def main():
    index = LocalIndex(os.environ["AKB_EXPECTED_KB_ID"])
    from akb.mcp_server import call

    index.scan(sys.argv[1], api=call)


if __name__ == "__main__":
    main()
