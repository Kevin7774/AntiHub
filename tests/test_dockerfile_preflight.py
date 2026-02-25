import unittest
from pathlib import Path
from unittest import mock

import worker
from dockerfile_parser import parse_dockerfile_from

FIXTURE = Path(__file__).parent / "fixtures" / "Dockerfile.multi-stage"


class DockerfilePreflightTests(unittest.TestCase):
    def test_parse_multistage_images_and_stages(self) -> None:
        info = parse_dockerfile_from(FIXTURE)
        self.assertEqual(
            info.external_images,
            ["python:alpine", "node:18-alpine", "nginx:alpine"],
        )
        self.assertEqual(info.stages, ["base", "app-base", "test", "dev"])
        self.assertTrue(info.requires_buildkit)

    def test_preflight_pull_images_only(self) -> None:
        info = parse_dockerfile_from(FIXTURE)
        client = mock.Mock()
        client.images.pull = mock.Mock()
        worker._pull_base_images(client, info.external_images, case_id=None)
        pulled = [call.args[0] for call in client.images.pull.call_args_list]
        self.assertEqual(pulled, info.external_images)
        self.assertNotIn("base", pulled)
        self.assertNotIn("app-base", pulled)


if __name__ == "__main__":
    unittest.main()
