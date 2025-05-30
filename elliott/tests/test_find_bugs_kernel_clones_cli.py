from io import StringIO
from unittest import IsolatedAsyncioTestCase
from unittest.mock import ANY, MagicMock, Mock, patch

import koji
from artcommonlib.assembly import AssemblyTypes
from elliottlib import early_kernel
from elliottlib.bzutil import JIRABugTracker
from elliottlib.cli.find_bugs_kernel_clones_cli import FindBugsKernelClonesCli
from elliottlib.config_model import KernelBugSweepConfig
from jira import JIRA, Issue


class TestFindBugsKernelClonesCli(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._config = KernelBugSweepConfig.model_validate(
            {
                "tracker_jira": {
                    "project": "KMAINT",
                    "labels": ["early-kernel-track"],
                },
                "bugzilla": {
                    "target_releases": ["9.2.0"],
                },
                "target_jira": {
                    "project": "OCPBUGS",
                    "component": "RHCOS",
                    "version": "4.14",
                    "target_release": "4.14.0",
                    "candidate_brew_tag": "rhaos-4.14-rhel-9-candidate",
                    "prod_brew_tag": "rhaos-4.14-rhel-9",
                },
            }
        )

    def test_get_jira_bugs(self):
        runtime = MagicMock()
        cli = FindBugsKernelClonesCli(
            runtime=runtime, trackers=[], bugs=[], move=True, update_tracker=True, dry_run=False
        )
        jira_client = MagicMock(spec=JIRA)
        component = MagicMock()
        component.configure_mock(name="RHCOS")
        target_release = MagicMock()
        target_release.configure_mock(name="4.14.0")
        jira_client.issue.side_effect = lambda key: MagicMock(
            spec=Issue,
            **{
                "key": key,
                "fields": MagicMock(),
                "fields.labels": ["art:cloned-kernel-bug"],
                "fields.project.key": "OCPBUGS",
                "fields.components": [component],
                f"fields.{JIRABugTracker.field_target_version}": [target_release],
            },
        )
        actual = cli._get_jira_bugs(jira_client, ["FOO-1", "FOO-2", "FOO-3"], self._config)
        self.assertEqual([bug.key for bug in actual], ["FOO-1", "FOO-2", "FOO-3"])

    def test_search_for_jira_bugs(self):
        jira_client = MagicMock(spec=JIRA)
        trackers = ["TRACKER-1", "TRACKER-2"]
        jira_client.search_issues.return_value = [
            MagicMock(key="FOO-1"),
            MagicMock(key="FOO-2"),
            MagicMock(key="FOO-3"),
        ]
        actual = FindBugsKernelClonesCli._search_for_jira_bugs(jira_client, trackers, self._config)
        expected_jql = 'labels = art:cloned-kernel-bug AND project = OCPBUGS AND component = RHCOS AND "Target Version" = "4.14.0" AND (labels = art:kmaint:TRACKER-1 OR labels = art:kmaint:TRACKER-2) order by created DESC'
        jira_client.search_issues.assert_called_once_with(expected_jql, maxResults=0)
        self.assertEqual([issue.key for issue in actual], ["FOO-1", "FOO-2", "FOO-3"])

    @patch("elliottlib.early_kernel.process_shipped_tracker")
    @patch("elliottlib.early_kernel.move_jira")
    @patch("elliottlib.brew.get_builds_tags")
    def test_update_jira_bugs(self, get_builds_tags: Mock, _move_jira: Mock, process_shipped_tracker: Mock):
        runtime = MagicMock()
        jira_client = MagicMock(spec=JIRA)
        tracker = jira_client.issue.return_value = MagicMock(
            spec=Issue,
            **{
                "key": "KMAINT-1",
                "fields": MagicMock(),
                "fields.project.key": "KMAINT",
                "fields.labels": ['early-kernel-track'],
                "fields.summary": "kernel-1.0.1-1.fake and kernel-rt-1.0.1-1.fake early delivery via OCP",
                "fields.description": "Fixes bugzilla.redhat.com/show_bug.cgi?id=5 and bz6.",
            },
        )
        cli = FindBugsKernelClonesCli(
            runtime=runtime, trackers=[], bugs=[], move=True, update_tracker=True, dry_run=False
        )
        bugs = [
            MagicMock(
                spec=Issue,
                **{
                    "key": "FOO-1",
                    "fields": MagicMock(),
                    "fields.labels": ["art:bz#1", "art:kmaint:KMAINT-1"],
                    "fields.status.name": "New",
                },
            ),
            MagicMock(
                spec=Issue,
                **{
                    "key": "FOO-2",
                    "fields": MagicMock(),
                    "fields.labels": ["art:bz#2", "art:kmaint:KMAINT-1"],
                    "fields.status.name": "Assigned",
                },
            ),
            MagicMock(
                spec=Issue,
                **{
                    "key": "FOO-3",
                    "fields": MagicMock(),
                    "fields.labels": ["art:bz#3", "art:kmaint:KMAINT-1"],
                    "fields.status.name": "ON_QA",
                },
            ),
        ]
        koji_api = MagicMock(spec=koji.ClientSession)

        get_builds_tags.return_value = [
            [{"name": "irrelevant-1"}, {"name": "rhaos-4.14-rhel-9-candidate"}],
            [{"name": "irrelevant-2"}, {"name": "rhaos-4.14-rhel-9-candidate"}],
        ]
        cli._update_jira_bugs(jira_client, bugs, koji_api, self._config)
        _move_jira.assert_any_call(ANY, False, jira_client, bugs[0], "MODIFIED", ANY)
        _move_jira.assert_any_call(ANY, False, jira_client, bugs[1], "MODIFIED", ANY)

        # now with shipped
        _move_jira.reset_mock()
        get_builds_tags.return_value = [
            [{"name": "rhaos-4.14-rhel-9"}, {"name": "rhaos-4.14-rhel-9-candidate"}],
            [{"name": "rhaos-4.14-rhel-9"}, {"name": "rhaos-4.14-rhel-9-candidate"}],
        ]
        cli._update_jira_bugs(jira_client, bugs, koji_api, self._config)
        _move_jira.assert_any_call(ANY, False, jira_client, bugs[0], "CLOSED", ANY)
        process_shipped_tracker.assert_called_once_with(ANY, False, ANY, tracker, ANY, "rhaos-4.14-rhel-9")

    def test_print_report(self):
        report = {
            "jira_issues": [
                {"key": "FOO-1", "summary": "test bug 1", "status": "Verified"},
                {"key": "FOO-2", "summary": "test bug 2", "status": "ON_QA"},
            ],
        }
        out = StringIO()
        FindBugsKernelClonesCli._print_report(report, out=out)
        self.assertEqual(
            out.getvalue().strip(),
            """
FOO-1	Verified	test bug 1
FOO-2	ON_QA	test bug 2
""".strip(),
        )

    @patch("elliottlib.cli.find_bugs_kernel_clones_cli.FindBugsKernelClonesCli._print_report")
    @patch("elliottlib.cli.find_bugs_kernel_clones_cli.FindBugsKernelClonesCli._get_jira_bugs")
    @patch("elliottlib.cli.find_bugs_kernel_clones_cli.FindBugsKernelClonesCli._update_jira_bugs")
    @patch("elliottlib.cli.find_bugs_kernel_clones_cli.FindBugsKernelClonesCli._search_for_jira_bugs")
    async def test_run_without_specified_bugs(
        self, _search_for_jira_bugs: Mock, _update_jira_bugs: Mock, _get_jira_bugs: Mock, _print_report: Mock
    ):
        runtime = MagicMock(assembly_type=AssemblyTypes.STREAM)
        runtime.gitdata.load_data.return_value = MagicMock(
            data={
                "kernel_bug_sweep": {
                    "tracker_jira": {
                        "project": "KMAINT",
                        "labels": ["early-kernel-track"],
                    },
                    "bugzilla": {
                        "target_releases": ["9.2.0"],
                    },
                    "target_jira": {
                        "project": "OCPBUGS",
                        "version": "4.14",
                        "target_release": "4.14.0",
                        "candidate_brew_tag": "rhaos-4.14-rhel-9-candidate",
                        "prod_brew_tag": "rhaos-4.14-rhel-9",
                    },
                },
            },
        )
        found_bugs = [
            MagicMock(
                spec=Issue,
                **{
                    "key": "FOO-1",
                    "fields": MagicMock(),
                    "fields.summary": "Fake bug 1",
                    "fields.labels": ["art:bz#1", "art:kmaint:KMAINT-1"],
                    "fields.status.name": "New",
                },
            ),
            MagicMock(
                spec=Issue,
                **{
                    "key": "FOO-2",
                    "fields": MagicMock(),
                    "fields.summary": "Fake bug 2",
                    "fields.labels": ["art:bz#2", "art:kmaint:KMAINT-1"],
                    "fields.status.name": "Assigned",
                },
            ),
            MagicMock(
                spec=Issue,
                **{
                    "key": "FOO-3",
                    "fields": MagicMock(),
                    "fields.summary": "Fake bug 3",
                    "fields.labels": ["art:bz#3", "art:kmaint:KMAINT-1"],
                    "fields.status.name": "ON_QA",
                },
            ),
        ]
        _search_for_jira_bugs.return_value = found_bugs
        cli = FindBugsKernelClonesCli(
            runtime=runtime, trackers=[], bugs=[], move=True, update_tracker=True, dry_run=False
        )
        cli.run()
        _update_jira_bugs.assert_called_once_with(ANY, found_bugs, ANY, ANY)
        expected_report = {
            'jira_issues': [
                {'key': 'FOO-1', 'summary': 'Fake bug 1', 'status': 'New'},
                {'key': 'FOO-2', 'summary': 'Fake bug 2', 'status': 'Assigned'},
                {'key': 'FOO-3', 'summary': 'Fake bug 3', 'status': 'ON_QA'},
            ],
        }
        _print_report.assert_called_once_with(expected_report, ANY)

    @patch("elliottlib.cli.find_bugs_kernel_clones_cli.FindBugsKernelClonesCli._print_report")
    @patch("elliottlib.cli.find_bugs_kernel_clones_cli.FindBugsKernelClonesCli._get_jira_bugs")
    @patch("elliottlib.cli.find_bugs_kernel_clones_cli.FindBugsKernelClonesCli._update_jira_bugs")
    @patch("elliottlib.cli.find_bugs_kernel_clones_cli.FindBugsKernelClonesCli._search_for_jira_bugs")
    async def test_run_with_specified_bugs(
        self, _search_for_jira_bugs: Mock, _update_jira_bugs: Mock, _get_jira_bugs: Mock, _print_report: Mock
    ):
        runtime = MagicMock(assembly_type=AssemblyTypes.STREAM)
        runtime.gitdata.load_data.return_value = MagicMock(
            data={
                "kernel_bug_sweep": {
                    "tracker_jira": {
                        "project": "KMAINT",
                        "labels": ["early-kernel-track"],
                    },
                    "bugzilla": {
                        "target_releases": ["9.2.0"],
                    },
                    "target_jira": {
                        "project": "OCPBUGS",
                        "version": "4.14",
                        "target_release": "4.14.0",
                        "candidate_brew_tag": "rhaos-4.14-rhel-9-candidate",
                        "prod_brew_tag": "rhaos-4.14-rhel-9",
                    },
                },
            },
        )
        found_bugs = [
            MagicMock(
                spec=Issue,
                **{
                    "key": "FOO-1",
                    "fields": MagicMock(),
                    "fields.summary": "Fake bug 1",
                    "fields.labels": ["art:bz#1", "art:kmaint:KMAINT-1"],
                    "fields.status.name": "New",
                },
            ),
            MagicMock(
                spec=Issue,
                **{
                    "key": "FOO-2",
                    "fields": MagicMock(),
                    "fields.summary": "Fake bug 2",
                    "fields.labels": ["art:bz#2", "art:kmaint:KMAINT-1"],
                    "fields.status.name": "Assigned",
                },
            ),
            MagicMock(
                spec=Issue,
                **{
                    "key": "FOO-3",
                    "fields": MagicMock(),
                    "fields.summary": "Fake bug 3",
                    "fields.labels": ["art:bz#3", "art:kmaint:KMAINT-1"],
                    "fields.status.name": "ON_QA",
                },
            ),
        ]
        _get_jira_bugs.return_value = found_bugs
        cli = FindBugsKernelClonesCli(
            runtime=runtime,
            trackers=[],
            bugs=["FOO-1", "FOO-2", "FOO-3"],
            move=True,
            update_tracker=True,
            dry_run=False,
        )
        cli.run()
        _update_jira_bugs.assert_called_once_with(ANY, found_bugs, ANY, ANY)
        expected_report = {
            'jira_issues': [
                {'key': 'FOO-1', 'summary': 'Fake bug 1', 'status': 'New'},
                {'key': 'FOO-2', 'summary': 'Fake bug 2', 'status': 'Assigned'},
                {'key': 'FOO-3', 'summary': 'Fake bug 3', 'status': 'ON_QA'},
            ],
        }
        _print_report.assert_called_once_with(expected_report, ANY)
