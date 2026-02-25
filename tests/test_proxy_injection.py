import os
import unittest
from unittest import mock

from config import get_proxy_config
from docker_ops import build_proxy_args


class ProxyInjectionTests(unittest.TestCase):
    def test_build_args_include_proxy(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "HTTP_PROXY": "http://host.docker.internal:7897",
                "NO_PROXY": "localhost,127.0.0.1",
            },
            clear=False,
        ):
            proxy_config = get_proxy_config()
            build_args = build_proxy_args(proxy_config)

        self.assertEqual(
            build_args.get("HTTP_PROXY"), "http://host.docker.internal:7897"
        )
        self.assertEqual(
            build_args.get("http_proxy"), "http://host.docker.internal:7897"
        )
        self.assertEqual(build_args.get("NO_PROXY"), "localhost,127.0.0.1")
        self.assertEqual(build_args.get("no_proxy"), "localhost,127.0.0.1")


if __name__ == "__main__":
    unittest.main()
