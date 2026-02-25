import asyncio
import unittest
from unittest import mock

import main


class TemplateContextPathTests(unittest.TestCase):
    def test_template_context_path_applied(self) -> None:
        with mock.patch.object(main, "set_case") as set_case_mock:
            with mock.patch.object(main, "build_and_run") as build_and_run_mock:
                build_and_run_mock.delay = mock.Mock()
                payload = main.CaseCreateRequest(template_id="node-bulletin-board")
                response = asyncio.run(main.create_case(payload))
        self.assertEqual(response.context_path, "bulletin-board-app")
        self.assertIsNone(response.dockerfile_path)
        set_case_mock.assert_called_once()
        build_and_run_mock.delay.assert_called_once()


if __name__ == "__main__":
    unittest.main()
