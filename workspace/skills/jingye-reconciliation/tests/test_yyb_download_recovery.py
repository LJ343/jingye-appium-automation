from __future__ import annotations

import ast
import json
import re
import unittest
from pathlib import Path
from types import SimpleNamespace


SCRIPT_PATH = Path(__file__).resolve().parents[1] / 'scripts' / 'yyb_search.py'
SCRIPT_TREE = ast.parse(SCRIPT_PATH.read_text(encoding='utf-8'))


def load_function(name: str, namespace: dict):
    function = next(
        node
        for node in SCRIPT_TREE.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    )
    module = ast.Module(body=[function], type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(SCRIPT_PATH), 'exec'), namespace)
    return namespace[name]


def load_functions(names, namespace: dict):
    wanted = set(names)
    functions = [
        node
        for node in SCRIPT_TREE.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in wanted
    ]
    if {node.name for node in functions} != wanted:
        raise LookupError(f'Functions not found: {wanted}')
    module = ast.Module(body=functions, type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(SCRIPT_PATH), 'exec'), namespace)
    return {name: namespace[name] for name in names}


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def time(self) -> float:
        return self.now

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeElement:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.clicked_at = None

    def is_displayed(self) -> bool:
        return True

    def is_enabled(self) -> bool:
        return True

    def click(self) -> None:
        self.clicked_at = self.clock.now


class FakeSwitcher:
    def __init__(self) -> None:
        self.contexts = []

    def context(self, name: str) -> None:
        self.contexts.append(name)


class FakeDriver:
    def __init__(self, element: FakeElement, contexts=()) -> None:
        self.element = element
        self.contexts = list(contexts)
        self.switch_to = FakeSwitcher()
        self.find_calls = 0

    def find_elements(self, _by, _value):
        self.find_calls += 1
        return [self.element]


def download_function(clock: FakeClock):
    namespace = {
        'AppiumBy': SimpleNamespace(ANDROID_UIAUTOMATOR='uia'),
        'By': SimpleNamespace(ID='id'),
        'time': clock,
        'ensure_native_context': lambda: None,
        'raise_if_appium_session_lost': lambda _exc, _label: None,
    }
    return load_function('try_click_download_button', namespace)


class BrowserPdfReadinessTest(unittest.TestCase):
    def test_native_button_waits_two_seconds_and_rebinds_before_click(self) -> None:
        clock = FakeClock()
        element = FakeElement(clock)
        driver = FakeDriver(element)

        clicked = download_function(clock)(driver, timeout=20, stable_delay=2.0)

        self.assertTrue(clicked)
        self.assertEqual(element.clicked_at, 2.0)
        self.assertGreaterEqual(driver.find_calls, 2)

    def test_webview_fallback_uses_the_same_stable_wait_and_rebind(self) -> None:
        clock = FakeClock()
        element = FakeElement(clock)
        driver = FakeDriver(element, contexts=('WEBVIEW_browser',))

        clicked = download_function(clock)(driver, timeout=0, stable_delay=2.0)

        self.assertTrue(clicked)
        self.assertEqual(element.clicked_at, 2.0)
        self.assertEqual(driver.find_calls, 2)
        self.assertIn('NATIVE_APP', driver.switch_to.contexts)


class RetryPolicyTest(unittest.TestCase):
    def test_first_and_second_download_attempts_share_slow_load_profile(self) -> None:
        function = next(
            node
            for node in SCRIPT_TREE.body
            if isinstance(node, ast.FunctionDef) and node.name == 'execute_single_download'
        )
        assignment = next(
            node
            for node in function.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == 'retry_profiles' for target in node.targets)
        )
        profiles = ast.literal_eval(assignment.value)

        self.assertEqual(profiles[0], (18, 20, 4, 45))
        self.assertEqual(profiles[1], profiles[0])
        self.assertEqual(profiles[2], (25, 30, 5, 60))

    def test_account_gets_three_complete_attempts_before_fatal_failure(self) -> None:
        assignment = next(
            node
            for node in SCRIPT_TREE.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name)
                and target.id == 'MAX_ACCOUNT_RECOVERY_RETRIES'
                for target in node.targets
            )
        )
        self.assertEqual(ast.literal_eval(assignment.value), 2)

    def test_freight_entry_uses_three_rounds(self) -> None:
        function = next(
            node
            for node in SCRIPT_TREE.body
            if isinstance(node, ast.FunctionDef)
            and node.name == 'enter_freight_confirmation_page'
        )
        self.assertEqual(ast.literal_eval(function.args.defaults[-1]), 3)

    def test_sales_entry_keeps_navigation_capacity_but_stalls_after_three_attempts(self) -> None:
        for function_name in ('enter_sales_page', 'enter_sales_page_without_query'):
            function = next(
                node
                for node in SCRIPT_TREE.body
                if isinstance(node, ast.FunctionDef) and node.name == function_name
            )
            self.assertEqual(ast.literal_eval(function.args.defaults[-1]), 6)

        assignment = next(
            node
            for node in SCRIPT_TREE.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name)
                and target.id == 'MODULE_ENTRY_STALLED_MAX_ATTEMPTS'
                for target in node.targets
            )
        )
        self.assertEqual(ast.literal_eval(assignment.value), 3)


class FakeAccountSessionSuspectedError(RuntimeError):
    pass


class FakeFreightTechnicalError(RuntimeError):
    pass


class FakeStageRecoveryExhaustedError(RuntimeError):
    pass


class FakeTransientUiError(RuntimeError):
    pass


class AccountReloginClassificationTest(unittest.TestCase):
    def relogin_function(self):
        return load_function(
            'is_account_relogin_error',
            {
                'AccountSessionSuspectedError': FakeAccountSessionSuspectedError,
                'TRANSIENT_UI_ERRORS': (FakeTransientUiError,),
                'is_appium_session_lost': lambda _exc: False,
            },
        )

    def test_entry_and_transient_ui_failures_require_relogin(self) -> None:
        should_relogin = self.relogin_function()

        self.assertTrue(should_relogin(FakeAccountSessionSuspectedError('entry')))
        self.assertTrue(should_relogin(FakeTransientUiError('purchase timeout')))

    def test_nested_entry_failure_requires_relogin_but_business_error_does_not(self) -> None:
        should_relogin = self.relogin_function()
        outer = RuntimeError('outer')
        outer.__cause__ = FakeAccountSessionSuspectedError('entry')

        self.assertTrue(should_relogin(outer))
        self.assertFalse(should_relogin(ValueError('business data is invalid')))

    def test_stage_recovery_exhaustion_is_not_amplified_by_account_retry(self) -> None:
        should_retry = load_function(
            'should_retry_account_after_error',
            {
                'MAX_ACCOUNT_RECOVERY_RETRIES': 2,
                'StageRecoveryExhaustedError': FakeStageRecoveryExhaustedError,
                'is_account_relogin_error': lambda _exc: True,
            },
        )

        self.assertTrue(should_retry(FakeAccountSessionSuspectedError('entry'), 1))
        self.assertTrue(should_retry(FakeAccountSessionSuspectedError('entry'), 2))
        self.assertFalse(should_retry(FakeAccountSessionSuspectedError('entry'), 3))
        self.assertFalse(should_retry(FakeStageRecoveryExhaustedError('stage'), 1))


class FreightScopeReloginRecoveryTest(unittest.TestCase):
    def test_entry_failure_relogs_immediately_and_resumes_same_scope(self) -> None:
        actions = []
        scope_args = (
            '销售单-河北众沃',
            'login_account',
            '10000000005',
            '冀Z00001',
        )
        process_calls = 0

        def process_scope(*args):
            nonlocal process_calls
            process_calls += 1
            actions.append(('process', args))
            if process_calls == 1:
                raise FakeAccountSessionSuspectedError('entry did not open')
            return 7

        function = load_function(
            'run_freight_scope_with_recovery',
            {
                'FREIGHT_SCOPE_MAX_ATTEMPTS': 3,
                'FreightTechnicalError': FakeFreightTechnicalError,
                'process_freight_scope': process_scope,
                'is_appium_session_lost': lambda _exc: False,
                'is_account_relogin_error': lambda exc: isinstance(
                    exc, FakeAccountSessionSuspectedError
                ),
                'dump_freight_debug_artifacts': lambda _label: None,
                'recreate_appium_session': lambda _label: actions.append('recreate'),
                'restart_app_keep_state': lambda: actions.append('restart'),
                'login_with_account': lambda phone: actions.append(('login', phone)),
                'activate_tizi_and_wait': lambda **_kwargs: actions.append('activate'),
                'get_freight_entry_state': lambda: 'park_icon_ready',
                'time': SimpleNamespace(sleep=lambda _seconds: None),
            },
        )

        result = function('10000000005', *scope_args)

        self.assertEqual(result, 7)
        self.assertEqual(
            actions,
            [
                ('process', scope_args),
                'recreate',
                'restart',
                ('login', '10000000005'),
                ('process', scope_args),
            ],
        )


class BrowserPageDriver:
    def __init__(self, package: str) -> None:
        self.current_package = package
        self.current_activity = 'com.honor.browser.ui.web.WebPageActivity'
        self.back_calls = 0

    def find_elements(self, _by, _value):
        return []

    def back(self) -> None:
        self.back_calls += 1
        self.current_package = 'com.ibb.tizi'
        self.current_activity = 'com.ibb.tizi.activity.ViewPoundsActivity'


class UpdatedHonorElement:
    def __init__(self, driver, kind: str) -> None:
        self.driver = driver
        self.kind = kind
        self.id = kind

    def get_attribute(self, attribute: str) -> str:
        if self.kind == 'url_bar':
            if attribute == 'text':
                return self.driver.pdf_title
            if attribute in {'contentDescription', 'content-desc', 'name'}:
                return self.driver.pdf_url
        if self.kind in {'window_control', 'toolbar'} and attribute in {
            'contentDescription',
            'content-desc',
            'name',
            'text',
        }:
            return f'{self.driver.next_tab_count()} 个窗口'
        return ''

    @property
    def text(self) -> str:
        return self.get_attribute('text')

    def is_displayed(self) -> bool:
        return True

    def is_enabled(self) -> bool:
        return True


class UpdatedHonorPdfDriver(BrowserPageDriver):
    PDF_TITLE = '8026091100325.pdf'
    PDF_URL = (
        'http://doc.hbjyjt.com:16093/download/doc'
        '?downloadKey=fixture-current-target'
    )
    TARGET_ID = 'A1B2C3D4'

    def __init__(
        self,
        clock: FakeClock,
        *,
        tab_titles=None,
        current_tab_index=None,
        start_activity='com.honor.browser.download.DownloadTaskForAdPageActivity',
        transition_delay=0.0,
    ) -> None:
        super().__init__('com.honor.browser')
        self.clock = clock
        self._current_activity = start_activity
        self.transition_delay = transition_delay
        self.transition_at = None
        self.pdf_title = self.PDF_TITLE
        self.pdf_url = self.PDF_URL
        self.tab_titles = list(tab_titles or ['主页', self.PDF_TITLE])
        self.current_tab_index = (
            len(self.tab_titles) - 1
            if current_tab_index is None
            else current_tab_index
        )
        self.back_times = []
        self.target_close_calls = []
        self.closed_titles = []
        self.target_close_error = None
        self.remove_tab_on_target_close = True
        self.target_close_sent = False
        self.post_close_counts = None
        self.find_values = []
        self.leave_browser_on_back = False

    @property
    def current_activity(self):
        if (
            self.transition_at is not None
            and self.clock.monotonic() >= self.transition_at
        ):
            self._current_activity = 'com.honor.browser.ui.web.WebPageActivity'
            self.transition_at = None
        return self._current_activity

    @current_activity.setter
    def current_activity(self, value):
        self._current_activity = value

    @property
    def window_count(self):
        return len(self.tab_titles)

    def next_tab_count(self):
        if self.target_close_sent and self.post_close_counts:
            return self.post_close_counts.pop(0)
        return len(self.tab_titles)

    def find_elements(self, _by, value):
        self.find_values.append(value)
        if self.current_activity.endswith('WebPageActivity'):
            if value.endswith('/url_bar'):
                return [UpdatedHonorElement(self, 'url_bar')]
            if value.endswith('/tab_switcher'):
                return [UpdatedHonorElement(self, 'window_control')]
        if self.current_activity.endswith('BrowserMainActivity'):
            if value.endswith('/toolbar_root'):
                return [UpdatedHonorElement(self, 'toolbar')]
        return []

    def close_current_target(self, expected_url, _timeout):
        self.target_close_calls.append(expected_url)
        if self.target_close_error is not None:
            raise self.target_close_error
        if expected_url != self.PDF_URL:
            raise RuntimeError(f'unexpected target URL: {expected_url}')
        self.target_close_sent = True
        if self.remove_tab_on_target_close:
            self.closed_titles.append(
                self.tab_titles.pop(self.current_tab_index)
            )
        self.pdf_title = ''
        self.pdf_url = ''
        self.current_activity = 'com.honor.browser.BrowserMainActivity'
        return self.TARGET_ID

    def back(self) -> None:
        self.back_calls += 1
        self.back_times.append(self.clock.monotonic())
        if self.leave_browser_on_back:
            self.current_package = 'com.ibb.tizi'
            self.current_activity = 'com.ibb.tizi.activity.ViewPoundsActivity'
            return
        if 'DownloadTask' in self.current_activity:
            if self.transition_delay:
                self.transition_at = self.clock.monotonic() + self.transition_delay
            else:
                self.current_activity = 'com.honor.browser.ui.web.WebPageActivity'
            return
        self.current_activity = 'com.honor.browser.BrowserMainActivity'


def browser_reclaim_function(clock: FakeClock):
    holder = {}

    def close_target(expected_url, timeout=5.0):
        return holder['driver'].close_current_target(expected_url, timeout)

    namespace = {
        'AppiumBy': SimpleNamespace(ID='id'),
        're': re,
        'time': clock,
        'ensure_native_context': lambda: None,
        'raise_if_appium_session_lost': lambda _exc, _label: None,
        'close_honor_current_webview_target': close_target,
    }
    function = load_function('close_current_honor_pdf_page', namespace)

    def invoke(driver, *args, **kwargs):
        holder['driver'] = driver
        return function(driver, *args, **kwargs)

    return invoke
class BrowserPageReclaimTest(unittest.TestCase):
    def test_unidentified_browser_page_is_not_sent_back(self) -> None:
        clock = FakeClock()
        driver = BrowserPageDriver('com.honor.browser')

        reclaimed = browser_reclaim_function(clock)(driver)

        self.assertFalse(reclaimed)
        self.assertEqual(driver.back_calls, 0)

    def test_non_browser_foreground_is_never_sent_back(self) -> None:
        clock = FakeClock()
        driver = BrowserPageDriver('com.ibb.tizi')

        reclaimed = browser_reclaim_function(clock)(driver)

        self.assertFalse(reclaimed)
        self.assertEqual(driver.back_calls, 0)

    def test_browser_home_is_never_sent_back_or_closed(self) -> None:
        clock = FakeClock()
        driver = UpdatedHonorPdfDriver(
            clock,
            start_activity='com.honor.browser.BrowserMainActivity',
        )

        reclaimed = browser_reclaim_function(clock)(driver)

        self.assertFalse(reclaimed)
        self.assertEqual(driver.back_calls, 0)
        self.assertEqual(driver.target_close_calls, [])

    def test_current_pdf_target_closes_without_using_card_position(self) -> None:
        clock = FakeClock()
        driver = UpdatedHonorPdfDriver(
            clock,
            tab_titles=[UpdatedHonorPdfDriver.PDF_TITLE, '主页'],
            current_tab_index=0,
            start_activity='com.honor.browser.ui.web.WebPageActivity',
        )

        reclaimed = browser_reclaim_function(clock)(driver)

        self.assertTrue(reclaimed)
        self.assertEqual(driver.back_calls, 0)
        self.assertEqual(driver.target_close_calls, [UpdatedHonorPdfDriver.PDF_URL])
        self.assertEqual(driver.closed_titles, [UpdatedHonorPdfDriver.PDF_TITLE])
        self.assertEqual(driver.window_count, 1)

    def test_download_page_uses_one_back_then_closes_current_target(self) -> None:
        clock = FakeClock()
        driver = UpdatedHonorPdfDriver(clock)

        reclaimed = browser_reclaim_function(clock)(driver)

        self.assertTrue(reclaimed)
        self.assertEqual(driver.back_calls, 1)
        self.assertEqual(driver.target_close_calls, [UpdatedHonorPdfDriver.PDF_URL])
        self.assertEqual(driver.closed_titles, [UpdatedHonorPdfDriver.PDF_TITLE])
        self.assertEqual(driver.window_count, 1)

    def test_slow_download_transition_never_causes_second_back(self) -> None:
        clock = FakeClock()
        driver = UpdatedHonorPdfDriver(clock, transition_delay=4.0)

        reclaimed = browser_reclaim_function(clock)(driver)

        self.assertTrue(reclaimed)
        self.assertEqual(driver.back_calls, 1)
        self.assertGreaterEqual(clock.monotonic(), 4.0)
        self.assertEqual(driver.closed_titles, [UpdatedHonorPdfDriver.PDF_TITLE])

    def test_package_exit_after_back_is_not_claimed_as_closed(self) -> None:
        clock = FakeClock()
        driver = UpdatedHonorPdfDriver(clock)
        driver.leave_browser_on_back = True

        reclaimed = browser_reclaim_function(clock)(driver)

        self.assertFalse(reclaimed)
        self.assertEqual(driver.back_calls, 1)
        self.assertEqual(driver.target_close_calls, [])

    def test_missing_pdf_url_never_calls_target_close(self) -> None:
        clock = FakeClock()
        driver = UpdatedHonorPdfDriver(
            clock,
            start_activity='com.honor.browser.ui.web.WebPageActivity',
        )
        driver.pdf_url = ''

        reclaimed = browser_reclaim_function(clock)(driver)

        self.assertFalse(reclaimed)
        self.assertEqual(driver.target_close_calls, [])
        self.assertEqual(driver.window_count, 2)

    def test_historical_duplicate_title_does_not_confuse_current_target(self) -> None:
        clock = FakeClock()
        driver = UpdatedHonorPdfDriver(
            clock,
            tab_titles=[
                UpdatedHonorPdfDriver.PDF_TITLE,
                '主页',
                UpdatedHonorPdfDriver.PDF_TITLE,
            ],
            current_tab_index=2,
            start_activity='com.honor.browser.ui.web.WebPageActivity',
        )

        reclaimed = browser_reclaim_function(clock)(driver)

        self.assertTrue(reclaimed)
        self.assertEqual(driver.closed_titles, [UpdatedHonorPdfDriver.PDF_TITLE])
        self.assertEqual(
            driver.tab_titles,
            [UpdatedHonorPdfDriver.PDF_TITLE, '主页'],
        )

    def test_eighty_two_tabs_do_not_trigger_recycler_or_card_access(self) -> None:
        clock = FakeClock()
        titles = [f'old-{index}.pdf' for index in range(81)]
        titles.insert(40, UpdatedHonorPdfDriver.PDF_TITLE)
        driver = UpdatedHonorPdfDriver(
            clock,
            tab_titles=titles,
            current_tab_index=40,
            start_activity='com.honor.browser.ui.web.WebPageActivity',
        )

        reclaimed = browser_reclaim_function(clock)(driver)

        self.assertTrue(reclaimed)
        self.assertEqual(driver.window_count, 81)
        self.assertFalse(
            any(
                value.endswith(('/tab_item_container', '/tab_title', '/action_root'))
                for value in driver.find_values
            )
        )

    def test_target_identity_failure_keeps_every_tab(self) -> None:
        clock = FakeClock()
        driver = UpdatedHonorPdfDriver(
            clock,
            start_activity='com.honor.browser.ui.web.WebPageActivity',
        )
        driver.target_close_error = RuntimeError('ambiguous attached target')

        reclaimed = browser_reclaim_function(clock)(driver)

        self.assertFalse(reclaimed)
        self.assertEqual(driver.target_close_calls, [UpdatedHonorPdfDriver.PDF_URL])
        self.assertEqual(driver.closed_titles, [])
        self.assertEqual(driver.window_count, 2)

    def test_single_window_is_never_closed(self) -> None:
        clock = FakeClock()
        driver = UpdatedHonorPdfDriver(
            clock,
            tab_titles=[UpdatedHonorPdfDriver.PDF_TITLE],
            current_tab_index=0,
            start_activity='com.honor.browser.ui.web.WebPageActivity',
        )

        reclaimed = browser_reclaim_function(clock)(driver)

        self.assertFalse(reclaimed)
        self.assertEqual(driver.target_close_calls, [])
        self.assertEqual(driver.window_count, 1)

    def test_transient_post_close_count_is_not_accepted_as_success(self) -> None:
        clock = FakeClock()
        driver = UpdatedHonorPdfDriver(
            clock,
            start_activity='com.honor.browser.ui.web.WebPageActivity',
        )
        driver.post_close_counts = ([1] * 6 + [2]) * 10

        reclaimed = browser_reclaim_function(clock)(driver)

        self.assertFalse(reclaimed)
        self.assertEqual(driver.target_close_calls, [UpdatedHonorPdfDriver.PDF_URL])
        self.assertEqual(driver.closed_titles, [UpdatedHonorPdfDriver.PDF_TITLE])
        self.assertEqual(driver.window_count, 1)

    def test_reclaim_source_has_no_virtual_tab_card_controls(self) -> None:
        function = next(
            node
            for node in SCRIPT_TREE.body
            if isinstance(node, ast.FunctionDef)
            and node.name == 'close_current_honor_pdf_page'
        )
        string_constants = {
            node.value
            for node in ast.walk(function)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }

        self.assertFalse(
            any(
                fragment in value
                for value in string_constants
                for fragment in ('tab_item_container', 'tab_title', 'action_root')
            )
        )

    def test_reclaim_call_precedes_force_stop_in_download_finally(self) -> None:
        function = next(
            node
            for node in SCRIPT_TREE.body
            if isinstance(node, ast.FunctionDef) and node.name == 'execute_single_download'
        )
        calls = [node for node in ast.walk(function) if isinstance(node, ast.Call)]
        reclaim_line = next(
            node.lineno
            for node in calls
            if isinstance(node.func, ast.Name)
            and node.func.id == 'close_current_honor_pdf_page'
        )
        terminate_line = next(
            node.lineno
            for node in calls
            if isinstance(node.func, ast.Attribute)
            and node.func.attr == 'terminate_app'
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == 'com.honor.browser'
        )
        self.assertLess(reclaim_line, terminate_line)

class FakeHttpResponse:
    def __init__(self, *, payload=None, text='') -> None:
        self.payload = payload
        self.text = text

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self.payload


class FakeDevtoolsSession:
    def __init__(
        self,
        *,
        ambiguous=False,
        wrong_package=False,
        change_before_close=False,
        replacement_after_close=False,
        historical_becomes_current=False,
    ) -> None:
        self.trust_env = True
        self.ambiguous = ambiguous
        self.wrong_package = wrong_package
        self.change_before_close = change_before_close
        self.replacement_after_close = replacement_after_close
        self.historical_becomes_current = historical_becomes_current
        self.closed = False
        self.close_sent = False
        self.urls = []
        self.json_calls = 0
        self.target_id = 'ABCDEF12'
        self.expected_url = UpdatedHonorPdfDriver.PDF_URL

    def get(self, url, timeout):
        self.urls.append((url, timeout))
        if url.endswith('/json/version'):
            return FakeHttpResponse(
                payload={
                    'Android-Package': (
                        'com.other.browser'
                        if self.wrong_package
                        else 'com.honor.browser'
                    )
                }
            )
        if '/json/close/' in url:
            self.close_sent = True
            return FakeHttpResponse(text='Target is closing')
        if url.endswith('/json'):
            self.json_calls += 1
            active_target_id = self.target_id
            if self.change_before_close and self.json_calls >= 2:
                active_target_id = 'ABCDEF13'
            attached = {
                'type': 'page',
                'url': self.expected_url,
                'id': active_target_id,
                'description': json.dumps({
                    'attached': True,
                    'empty': False,
                    'visible': True,
                    'width': 1080,
                    'height': 1973,
                }),
            }
            historical_same_url = {
                'type': 'page',
                'url': self.expected_url,
                'id': 'DEAD0001',
                'description': json.dumps({
                    'attached': False,
                    'empty': True,
                    'visible': True,
                    'width': 0,
                    'height': 0,
                }),
            }
            if self.close_sent and self.historical_becomes_current:
                historical_same_url['description'] = attached['description']
            targets = [historical_same_url]
            if not self.close_sent:
                targets.append(attached)
                if self.ambiguous:
                    duplicate = dict(attached)
                    duplicate['id'] = 'BAD00002'
                    targets.append(duplicate)
            elif self.replacement_after_close:
                replacement = dict(attached)
                replacement['id'] = 'FEED0003'
                targets.append(replacement)
            return FakeHttpResponse(payload=targets)
        raise AssertionError(f'Unexpected URL: {url}')

    def close(self):
        self.closed = True


class FakeRequests:
    def __init__(self, session) -> None:
        self.session = session

    def Session(self):
        return self.session


class FakeAdbSubprocess:
    def __init__(self, *, remove_failures=0, list_failures=0) -> None:
        self.calls = []
        self.remove_failures = remove_failures
        self.list_failures = list_failures
        self.remove_attempts = 0
        self.list_attempts = 0
        self.forward_active = False

    def run(self, arguments, **_kwargs):
        self.calls.append(list(arguments))
        command = arguments[3:]
        returncode = 0
        stderr = ''
        if command == ['shell', 'pidof', 'com.honor.browser']:
            stdout = '25886\n'
        elif command == ['shell', 'cat', '/proc/net/unix']:
            stdout = (
                '00000000: 00000002 00000000 00010000 0001 01 '
                '@webview_devtools_remote_25886\n'
            )
        elif command == [
            'forward',
            'tcp:0',
            'localabstract:webview_devtools_remote_25886',
        ]:
            self.forward_active = True
            stdout = '59201\n'
        elif command == ['forward', '--remove', 'tcp:59201']:
            self.remove_attempts += 1
            stdout = ''
            if self.remove_attempts <= self.remove_failures:
                returncode = 1
                stderr = 'temporary remove failure'
            else:
                self.forward_active = False
        elif command == ['forward', '--list']:
            self.list_attempts += 1
            if self.list_attempts <= self.list_failures:
                stdout = ''
                returncode = 1
                stderr = 'temporary list failure'
            elif self.forward_active:
                stdout = (
                    'YOUR_USB_ADB_SERIAL tcp:59201 '
                    'localabstract:webview_devtools_remote_25886\n'
                )
            else:
                stdout = ''
        else:
            raise AssertionError(f'Unexpected ADB command: {arguments}')
        return SimpleNamespace(
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )


def devtools_close_function(clock, session, fake_subprocess):
    namespace = {
        'ADB_SERIAL': 'YOUR_USB_ADB_SERIAL',
        'json': json,
        're': re,
        'requests': FakeRequests(session),
        'subprocess': fake_subprocess,
        'time': clock,
    }
    functions = load_functions(
        (
            '_run_adb_for_honor_reclaim',
            'close_honor_current_webview_target',
        ),
        namespace,
    )
    return functions['close_honor_current_webview_target']


class HonorDevtoolsTargetTest(unittest.TestCase):
    def test_closes_only_attached_nonempty_target_with_exact_current_url(self) -> None:
        clock = FakeClock()
        session = FakeDevtoolsSession()
        fake_subprocess = FakeAdbSubprocess()

        target_id = devtools_close_function(
            clock,
            session,
            fake_subprocess,
        )(UpdatedHonorPdfDriver.PDF_URL)

        self.assertEqual(target_id, session.target_id)
        self.assertFalse(session.trust_env)
        self.assertTrue(session.close_sent)
        self.assertTrue(session.closed)
        self.assertIn(
            ['adb', '-s', 'YOUR_USB_ADB_SERIAL', 'forward', '--remove', 'tcp:59201'],
            fake_subprocess.calls,
        )

    def test_ambiguous_attached_targets_are_not_closed_and_forward_is_removed(self) -> None:
        clock = FakeClock()
        session = FakeDevtoolsSession(ambiguous=True)
        fake_subprocess = FakeAdbSubprocess()

        with self.assertRaisesRegex(RuntimeError, '匹配数=2'):
            devtools_close_function(
                clock,
                session,
                fake_subprocess,
            )(UpdatedHonorPdfDriver.PDF_URL)

        self.assertFalse(session.close_sent)
        self.assertTrue(session.closed)
        self.assertIn(
            ['adb', '-s', 'YOUR_USB_ADB_SERIAL', 'forward', '--remove', 'tcp:59201'],
            fake_subprocess.calls,
        )

    def test_wrong_devtools_package_is_rejected_before_target_close(self) -> None:
        clock = FakeClock()
        session = FakeDevtoolsSession(wrong_package=True)
        fake_subprocess = FakeAdbSubprocess()

        with self.assertRaisesRegex(RuntimeError, '不属于 Honor 浏览器'):
            devtools_close_function(
                clock,
                session,
                fake_subprocess,
            )(UpdatedHonorPdfDriver.PDF_URL)

        self.assertFalse(session.close_sent)
        self.assertTrue(session.closed)

    def test_target_identity_change_before_close_is_rejected(self) -> None:
        clock = FakeClock()
        session = FakeDevtoolsSession(change_before_close=True)
        fake_subprocess = FakeAdbSubprocess()

        with self.assertRaisesRegex(RuntimeError, '关闭前身份已变化'):
            devtools_close_function(
                clock,
                session,
                fake_subprocess,
            )(UpdatedHonorPdfDriver.PDF_URL)

        self.assertFalse(session.close_sent)
        self.assertFalse(fake_subprocess.forward_active)

    def test_new_same_url_target_after_close_is_not_accepted(self) -> None:
        clock = FakeClock()
        session = FakeDevtoolsSession(replacement_after_close=True)
        fake_subprocess = FakeAdbSubprocess()

        with self.assertRaisesRegex(RuntimeError, '同 URL target'):
            devtools_close_function(
                clock,
                session,
                fake_subprocess,
            )(UpdatedHonorPdfDriver.PDF_URL)

        self.assertTrue(session.close_sent)
        self.assertFalse(fake_subprocess.forward_active)

    def test_existing_same_url_history_can_become_current_after_close(self) -> None:
        clock = FakeClock()
        session = FakeDevtoolsSession(historical_becomes_current=True)
        fake_subprocess = FakeAdbSubprocess()

        target_id = devtools_close_function(
            clock,
            session,
            fake_subprocess,
        )(UpdatedHonorPdfDriver.PDF_URL)

        self.assertEqual(target_id, session.target_id)
        self.assertFalse(fake_subprocess.forward_active)

    def test_forward_removal_is_retried_and_confirmed(self) -> None:
        clock = FakeClock()
        session = FakeDevtoolsSession()
        fake_subprocess = FakeAdbSubprocess(remove_failures=1)

        target_id = devtools_close_function(
            clock,
            session,
            fake_subprocess,
        )(UpdatedHonorPdfDriver.PDF_URL)

        self.assertEqual(target_id, session.target_id)
        self.assertEqual(fake_subprocess.remove_attempts, 2)
        self.assertGreaterEqual(fake_subprocess.list_attempts, 2)
        self.assertFalse(fake_subprocess.forward_active)



class FakeTimeout(Exception):
    pass


class FakeWait:
    def until(self, condition):
        result = condition(None)
        if result:
            return result
        raise FakeTimeout('timed out')


class SalesStateDriver:
    def __init__(self, recover_on_back: bool, advance_park_icon: bool = True) -> None:
        self.state = 'purchase_page'
        self.recover_on_back = recover_on_back
        self.advance_park_icon = advance_park_icon
        self.back_calls = 0
        self.park_icon_clicks = 0

    def back(self) -> None:
        self.back_calls += 1
        if self.recover_on_back:
            self.state = 'park_home'


class SalesClickElement:
    def __init__(self, driver: SalesStateDriver, locator) -> None:
        self.driver = driver
        self.locator = locator

    def click(self) -> None:
        resource_id = self.locator[1]
        if resource_id.endswith('/iv_left'):
            self.driver.state = 'unknown'
        elif resource_id.endswith('/rd_user_yq'):
            self.driver.state = 'park_icon_ready'
        elif resource_id.endswith('/icon_yuanqu_1'):
            self.driver.park_icon_clicks += 1
            if self.driver.advance_park_icon:
                self.driver.state = 'sales_page'


class FakeSalesEntryError(RuntimeError):
    pass


def sales_entry_function(driver: SalesStateDriver):
    def locator_visible(locator) -> bool:
        resource_id = locator[1]
        state = driver.state
        return (
            (state == 'purchase_page' and resource_id.endswith(('/start_date', '/end_date')))
            or (state == 'sales_page' and resource_id.endswith('/search'))
            or (state == 'park_icon_ready' and resource_id.endswith('/icon_yuanqu_1'))
            or (state == 'park_home' and resource_id.endswith('/rd_user_yq'))
            or (state == 'business_page' and resource_id.endswith('/image_main'))
            or (state == 'inner_page' and resource_id.endswith('/iv_left'))
        )

    namespace = {
        'AppiumBy': SimpleNamespace(ID='id'),
        'SalesEntryError': FakeSalesEntryError,
        'MODULE_ENTRY_STALLED_MAX_ATTEMPTS': 3,
        'TimeoutException': FakeTimeout,
        'TRANSIENT_UI_ERRORS': (FakeTimeout,),
        'driver': driver,
        'ensure_native_context': lambda: None,
        'locator_visible_now': locator_visible,
        'wait_clickable': lambda locator, timeout: SalesClickElement(driver, locator),
        'fast_wait': lambda _timeout: FakeWait(),
        'diagnose_current_app_page': lambda _label: {'package': 'com.ibb.tizi'},
        'activate_tizi_and_wait': lambda _locators, timeout: None,
        'raise_if_appium_session_lost': lambda _exc, _label: None,
        'time': SimpleNamespace(sleep=lambda _seconds: None),
        'click_step': lambda *_args, **_kwargs: None,
        'wait_nonempty_text': lambda _locator, timeout: '冀Z00008',
    }
    return load_function('enter_sales_page', namespace)


class SalesEntryRecoveryTest(unittest.TestCase):
    def test_one_bounded_system_back_recovers_unknown_app_layer(self) -> None:
        driver = SalesStateDriver(recover_on_back=True)

        car = sales_entry_function(driver)(max_rounds=6)

        self.assertEqual(car, '冀Z00008')
        self.assertEqual(driver.back_calls, 1)
        self.assertEqual(driver.state, 'sales_page')

    def test_persistent_unknown_is_fatal_instead_of_silently_skipped(self) -> None:
        driver = SalesStateDriver(recover_on_back=False)

        with self.assertRaises(FakeSalesEntryError):
            sales_entry_function(driver)(max_rounds=3)

        self.assertEqual(driver.back_calls, 1)

    def test_same_sales_entry_state_stalls_only_three_times_before_relogin(self) -> None:
        driver = SalesStateDriver(recover_on_back=False, advance_park_icon=False)
        driver.state = 'park_icon_ready'

        with self.assertRaises(FakeSalesEntryError):
            sales_entry_function(driver)(max_rounds=6)

        self.assertEqual(driver.park_icon_clicks, 3)


if __name__ == '__main__':
    unittest.main()
