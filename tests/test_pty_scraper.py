"""Tests for pty_scraper parsing functions and mocked scrape calls."""

import json
import os
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch
import urllib.error

import pytest

from src.pty_scraper import (
    _clean,
    _claude_web_request_config,
    _codex_analytics_helper_script,
    _merge_claude_browser_bundle,
    _parse_claude_web_usage_payload,
    _parse_codex_rate_limits_payload,
    _reset_epoch,
    _cursor_auth_token,
    _merge_cursor_limit_state,
    _parse_cursor_agent_limit_text,
    _find_bin,
    _strip_ansi,
    claude_web_usage_configured,
    codex_web_analytics_configured,
    scrape_claude_usage_web,
    scrape_codex_analytics,
    scrape_codex_usage,
    scrape_cursor_usage,
)


class TestStripAnsi:
    def test_removes_color_codes(self):
        assert _strip_ansi("\x1b[31mred\x1b[0m") == "red"

    def test_plain_text_unchanged(self):
        assert _strip_ansi("hello world") == "hello world"

    def test_removes_osc_sequences(self):
        # The regex strips partial OSC sequences; verify it does something
        result = _strip_ansi("\x1b]0;title\x07text")
        assert "text" in result


class TestClean:
    def test_removes_ansi_and_crlf(self):
        assert _clean("\x1b[32mhello\x1b[0m\r\nworld") == "hello\nworld"

    def test_cr_only(self):
        # _clean replaces \r\n with \n and \r with '', so standalone \r is removed
        result = _clean("a\rb")
        assert "\r" not in result


class TestResetEpoch:
    def test_parses_iso_and_millisecond_epoch(self):
        expected = int(datetime.fromisoformat("2026-07-30T17:31:00-07:00").timestamp())

        assert _reset_epoch("2026-07-30T17:31:00-07:00") == expected
        assert _reset_epoch(expected * 1000) == expected

    def test_parses_provider_display_time_with_year(self):
        result = _reset_epoch("Jul 30, 2026 at 5:31 PM")

        assert result is not None
        parsed = datetime.fromtimestamp(result)
        assert (parsed.year, parsed.month, parsed.day, parsed.hour, parsed.minute) == (
            2026,
            7,
            30,
            17,
            31,
        )

    def test_parses_provider_display_time_without_year(self):
        target = datetime.now() + timedelta(days=7)
        result = _reset_epoch(target.strftime("%b %d %I:%M %p"))

        assert result is not None
        parsed = datetime.fromtimestamp(result)
        assert (parsed.year, parsed.month, parsed.day, parsed.hour, parsed.minute) == (
            target.year,
            target.month,
            target.day,
            target.hour,
            target.minute,
        )

    def test_parses_time_only_as_next_occurrence(self):
        now = datetime.now()
        result = _reset_epoch(now.strftime("%I:%M %p"))

        assert result is not None
        parsed = datetime.fromtimestamp(result)
        assert now < parsed <= now + timedelta(days=1)


class TestFindBin:
    def test_finds_in_path(self):
        result = _find_bin("python3")
        assert result.endswith("python3")

    def test_nonexistent_falls_through(self):
        result = _find_bin("nonexistent_binary_xyz_12345")
        assert result == "nonexistent_binary_xyz_12345"


class TestScrapeClaudeUsageWeb:
    def test_keychain_cookie_takes_precedence_and_removes_managed_fallback(self, tmp_path):
        fallback = tmp_path / ".usage-tracker" / "claude-cookie.txt"
        fallback.parent.mkdir()
        fallback.write_text("sessionKey=legacy; lastActiveOrg=org-legacy")
        with (
            patch("src.pty_scraper.load_provider_credential", return_value="sessionKey=keychain; lastActiveOrg=org-keychain"),
            patch("pathlib.Path.home", return_value=tmp_path),
            patch.dict(
                os.environ,
                {
                    "CLAUDE_WEB_COOKIE": "sessionKey=environment; lastActiveOrg=org-environment",
                    "CLAUDE_WEB_ORG_ID": "",
                    "CLAUDE_WEB_HEADERS_JSON": "",
                    "CLAUDE_WEB_HEADERS_FILE": "",
                },
                clear=False,
            ),
        ):
            url, headers = _claude_web_request_config()

        assert url.endswith("/organizations/org-keychain/usage")
        assert "sessionKey=keychain" in headers["Cookie"]
        assert not fallback.exists()

    def test_incomplete_keychain_cookie_falls_back_to_environment(self):
        with (
            patch("src.pty_scraper.load_provider_credential", return_value="ARID=incomplete"),
            patch.dict(
                os.environ,
                {
                    "CLAUDE_WEB_COOKIE": "sessionKey=environment; lastActiveOrg=org-environment",
                    "CLAUDE_WEB_ORG_ID": "",
                    "CLAUDE_WEB_HEADERS_JSON": "",
                    "CLAUDE_WEB_HEADERS_FILE": "",
                },
                clear=False,
            ),
        ):
            url, headers = _claude_web_request_config()

        assert url.endswith("/organizations/org-environment/usage")
        assert "sessionKey=environment" in headers["Cookie"]

    def test_incomplete_cookie_is_not_configured(self):
        with (
            patch("src.pty_scraper.load_provider_credential", return_value="ARID=incomplete"),
            patch.dict(
                os.environ,
                {
                    "CLAUDE_WEB_COOKIE": "",
                    "CLAUDE_WEB_COOKIE_FILE": "",
                    "CLAUDE_WEB_ORG_ID": "",
                    "CLAUDE_WEB_HEADERS_JSON": "",
                    "CLAUDE_WEB_HEADERS_FILE": "",
                },
                clear=False,
            ),
        ):
            assert _claude_web_request_config() is None

    def test_config_detected_from_cookie_last_active_org(self):
        with patch.dict(
            os.environ,
            {
                "CLAUDE_WEB_COOKIE": "sessionKey=test-session; lastActiveOrg=org-123; cf_clearance=test",
                "CLAUDE_WEB_ORG_ID": "",
                "CLAUDE_WEB_HEADERS_JSON": "",
                "CLAUDE_WEB_HEADERS_FILE": "",
            },
            clear=False,
        ):
            assert claude_web_usage_configured() is True

    def test_parses_structured_usage_payload(self):
        payload = {
            "currentSession": {
                "usedPercent": 24,
                "resetAt": "2026-04-11T02:00:00Z",
            },
            "weekly": {
                "allModels": {
                    "usedPercent": 17,
                    "resetAt": "2026-04-17T06:00:00Z",
                },
                "sonnetOnly": {
                    "usedPercent": 0,
                },
                "designOnly": {
                    "usedPercent": 42,
                },
            },
            "extraUsage": {
                "usedPercent": 10,
                "spentUsd": 1.5,
                "limitUsd": 10.0,
                "balanceUsd": 8.5,
                "resetAt": "2026-05-01T07:00:00Z",
            },
        }

        result = _parse_claude_web_usage_payload(payload)
        assert result["session_pct"] == 24
        assert result["weekly_pct"] == 17
        assert result["weekly_sonnet_pct"] == 0
        assert result["weekly_design_pct"] == 42
        assert result["extra_pct"] == 10
        assert result["extra_spent_usd"] == 1.5
        assert result["extra_limit_usd"] == 10.0
        assert result["extra_balance_usd"] == 8.5
        assert result["session_reset"] == "Apr 10 7:00 PM"
        assert result["weekly_reset"] == "Apr 16 11:00 PM"
        assert len(result["limits"]) == 4
        assert result["credit_pools"][0]["balance"] == 8.5
        assert result["credit_pools"][0]["monthly_spend_headroom"] == 8.5

    def test_spend_headroom_is_not_reported_as_credit_balance(self):
        result = _parse_claude_web_usage_payload({
            "five_hour": {"utilization": 10},
            "seven_day": {"utilization": 20},
            "extra_usage": {
                "is_enabled": True,
                "monthly_limit": 50,
                "used_credits": 12,
            },
        })

        assert "extra_balance_usd" not in result
        pool = result["credit_pools"][0]
        assert pool["balance"] is None
        assert pool["monthly_spend_cap"] == 50
        assert pool["monthly_spend_headroom"] == 38

    def test_minor_currency_units_are_normalized(self):
        result = _parse_claude_web_usage_payload({
            "five_hour": {"utilization": 10},
            "seven_day": {"utilization": 20},
            "extra_usage": {
                "currency": "USD",
                "decimal_places": 2,
                "monthly_limit": 15000,
                "used_credits": 5566,
                "spend_limit_reached": False,
            },
        })

        assert result["extra_spent_usd"] == 55.66
        assert result["extra_limit_usd"] == 150.0
        pool = result["credit_pools"][0]
        assert pool["spent_month"] == 55.66
        assert pool["monthly_spend_cap"] == 150.0
        assert pool["monthly_spend_headroom"] == 94.34
        assert pool["spend_control_reached"] is False

    def test_preserves_unknown_model_or_feature_limit(self):
        result = _parse_claude_web_usage_payload({
            "five_hour": {"utilization": 10},
            "seven_day": {"utilization": 20},
            "thirty_day_opus": {"utilization": 35, "resets_at": "2026-08-01T00:00:00Z"},
        })

        bucket = next(item for item in result["limits"] if item["id"] == "claude-thirty-day-opus")
        assert bucket["window_kind"] == "monthly"
        assert bucket["scope_kind"] == "model"

    def test_prefers_current_session_reset_over_nested_weekly_reset(self):
        payload = {
            "currentSession": {
                "usedPercent": 24,
                "resetAt": "2026-04-11T02:00:00Z",
                "sevenDay": {
                    "resetAt": "2026-04-17T06:00:00Z",
                },
            },
            "weekly": {
                "allModels": {
                    "usedPercent": 17,
                    "resetAt": "2026-04-17T06:00:00Z",
                },
            },
        }

        result = _parse_claude_web_usage_payload(payload)

        assert result["session_pct"] == 24
        assert result["session_reset"] == "Apr 10 7:00 PM"
        assert result["weekly_pct"] == 17
        assert result["weekly_reset"] == "Apr 16 11:00 PM"

    def test_parses_live_claude_schema(self):
        payload = {
            "five_hour": {
                "utilization": 24.0,
                "resets_at": "2026-04-11T02:00:00.000000+00:00",
            },
            "seven_day": {
                "utilization": 17.0,
                "resets_at": "2026-04-17T06:00:00.000000+00:00",
            },
            "seven_day_sonnet": {
                "utilization": 0.0,
                "resets_at": "2026-04-17T06:00:00.000000+00:00",
            },
            "seven_day_design": {
                "utilization": 42.0,
                "resets_at": "2026-04-17T06:00:00.000000+00:00",
            },
            "extra_usage": {
                "is_enabled": False,
                "monthly_limit": None,
                "used_credits": None,
                "utilization": None,
            },
        }

        result = _parse_claude_web_usage_payload(payload)
        assert result["session_pct"] == 24
        assert result["weekly_pct"] == 17
        assert result["weekly_sonnet_pct"] == 0
        assert result["weekly_design_pct"] == 42
        assert result["session_reset"] == "Apr 10 7:00 PM"
        assert result["weekly_reset"] == "Apr 16 11:00 PM"

    def test_parses_fable_from_scoped_limits_array(self):
        payload = {
            "five_hour": {
                "utilization": 81,
                "resets_at": "2026-07-25T21:30:00Z",
            },
            "seven_day": {
                "utilization": 41,
                "resets_at": "2026-07-28T01:00:00Z",
            },
            "limits": [
                {
                    "kind": "weekly_scoped",
                    "group": "weekly",
                    "percent": 22,
                    "resets_at": "2026-07-28T01:00:00Z",
                    "scope": {
                        "model": {"id": None, "display_name": "Fable"},
                        "surface": None,
                    },
                }
            ],
        }

        result = _parse_claude_web_usage_payload(payload)

        assert result["weekly_pct"] == 41
        assert result["weekly_fable_pct"] == 22
        fable = next(item for item in result["limits"] if item["id"] == "claude-weekly-fable")
        assert fable["label"] == "Fable weekly"
        assert fable["scope_kind"] == "model"
        assert fable["model"] == "fable"
        assert fable["used_pct"] == 22
        assert fable["reset"] == "Jul 27 6:00 PM"

    def test_merges_spend_balance_and_promotional_credits(self):
        payload = _merge_claude_browser_bundle({
            "usage": {
                "five_hour": {"utilization": 81, "resets_at": "2026-07-25T21:30:00Z"},
                "seven_day": {"utilization": 41, "resets_at": "2026-07-28T01:00:00Z"},
                "extra_usage": {
                    "is_enabled": True,
                    "monthly_limit": 15000,
                    "used_credits": 5566,
                    "utilization": 37.106,
                    "currency": "USD",
                    "decimal_places": 2,
                },
            },
            "overage": {
                "data": {
                    "is_enabled": True,
                    "monthly_credit_limit": 15000,
                    "used_credits": 5566,
                    "currency": "USD",
                    "out_of_credits": False,
                },
                "error": None,
            },
            "prepaid": {
                "data": {
                    "amount": 24435,
                    "currency": "USD",
                    "next_expires_at": "2026-08-09T00:00:00Z",
                    "promo_tranches": [
                        {
                            "remaining_amount_minor_units": 14433,
                            "currency": "USD",
                            "expires_at": "2026-08-09T00:00:00Z",
                        },
                        {
                            "remaining_amount_minor_units": 10000,
                            "currency": "USD",
                            "expires_at": "2026-09-19T00:00:00Z",
                        },
                    ],
                },
                "error": None,
            },
        }, now=datetime(2026, 7, 25).astimezone())

        result = _parse_claude_web_usage_payload(payload)

        assert result["extra_spent_usd"] == 55.66
        assert result["extra_limit_usd"] == 150.0
        assert result["extra_balance_usd"] == 244.35
        assert result["credit_pools"][0]["balance"] == 244.35
        assert result["credit_pools"][0]["spent_month"] == 55.66
        assert result["credit_pools"][0]["monthly_spend_headroom"] == 94.34
        assert result["credit_pools"][0]["out_of_credits"] is False
        assert result["credit_pools"][0]["spend_control_reached"] is False
        assert result["credit_pools"][0]["reset"].startswith("Aug 1")
        assert result["credit_pools"][0]["next_expiry_at"] == 1786233600
        assert [pool["balance"] for pool in result["credit_pools"][1:]] == [144.33, 100.0]

    def test_distinguishes_empty_prepaid_balance_from_monthly_spend_cap(self):
        empty_balance = _parse_claude_web_usage_payload(_merge_claude_browser_bundle({
            "usage": {"five_hour": {"utilization": 100}},
            "overage": {
                "data": {
                    "is_enabled": True,
                    "monthly_credit_limit": 15000,
                    "used_credits": 8000,
                    "currency": "USD",
                    "out_of_credits": True,
                },
            },
            "prepaid": {"data": {"amount": 0, "currency": "USD"}},
        }))
        monthly_cap = _parse_claude_web_usage_payload(_merge_claude_browser_bundle({
            "usage": {"five_hour": {"utilization": 100}},
            "overage": {
                "data": {
                    "is_enabled": True,
                    "monthly_credit_limit": 15000,
                    "used_credits": 15000,
                    "currency": "USD",
                    "out_of_credits": True,
                },
            },
            "prepaid": {"data": {"amount": 20000, "currency": "USD"}},
        }))

        empty_pool = empty_balance["credit_pools"][0]
        assert empty_pool["out_of_credits"] is True
        assert empty_pool["balance"] == 0
        assert empty_pool["spend_control_reached"] is False

        cap_pool = monthly_cap["credit_pools"][0]
        assert cap_pool["out_of_credits"] is True
        assert cap_pool["balance"] == 200
        assert cap_pool["spend_control_reached"] is True

    def test_omits_missing_weekly_sublimits(self):
        payload = {
            "five_hour": {
                "utilization": 24.0,
                "resets_at": "2026-04-11T02:00:00.000000+00:00",
            },
            "seven_day": {
                "utilization": 17.0,
                "resets_at": "2026-04-17T06:00:00.000000+00:00",
            },
        }

        result = _parse_claude_web_usage_payload(payload)

        assert "weekly_sonnet_pct" not in result
        assert "weekly_design_pct" not in result

    def test_does_not_promote_weekly_sublimit_to_aggregate_weekly(self):
        payload = {
            "weekly": {
                "sonnetOnly": {
                    "usedPercent": 9,
                    "resetAt": "2026-04-17T06:00:00Z",
                },
            },
        }

        result = _parse_claude_web_usage_payload(payload)

        assert result["weekly_sonnet_pct"] == 9
        assert "weekly_pct" not in result
        assert "weekly_reset" not in result

    @patch("src.pty_scraper.urllib.request.urlopen")
    def test_fetches_usage_from_web_api(self, mock_urlopen):
        response = MagicMock()
        response.read.return_value = json.dumps({
            "currentSession": {"usedPercent": 24, "resetAt": "2026-04-11T02:00:00Z"},
            "weekly": {"allModels": {"usedPercent": 17, "resetAt": "2026-04-17T06:00:00Z"}},
        }).encode()
        response.headers = {"content-type": "application/json"}
        response.__enter__ = MagicMock(return_value=response)
        response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = response

        with patch.dict(
            os.environ,
            {
                "CLAUDE_WEB_COOKIE": "sessionKey=test-session; lastActiveOrg=org-123",
                "CLAUDE_WEB_ORG_ID": "",
                "CLAUDE_WEB_HEADERS_JSON": "",
                "CLAUDE_WEB_HEADERS_FILE": "",
                "CLAUDE_WEB_FETCH_MODE": "http",
            },
            clear=False,
        ):
            result = scrape_claude_usage_web()

        assert result["session_pct"] == 24
        assert result["weekly_pct"] == 17
        request = mock_urlopen.call_args[0][0]
        assert request.full_url == "https://claude.ai/api/organizations/org-123/usage"
        assert request.get_header("Cookie") == "sessionKey=test-session; lastActiveOrg=org-123"

    @patch("src.pty_scraper.urllib.request.urlopen")
    def test_cloudflare_challenge_is_reported(self, mock_urlopen):
        err = urllib.error.HTTPError(
            "https://claude.ai/api/organizations/org-123/usage",
            403,
            "Forbidden",
            {"cf-mitigated": "challenge"},
            BytesIO(b"Just a moment..."),
        )
        mock_urlopen.side_effect = err

        with patch.dict(
            os.environ,
            {
                "CLAUDE_WEB_COOKIE": "sessionKey=test-session; lastActiveOrg=org-123",
                "CLAUDE_WEB_ORG_ID": "",
                "CLAUDE_WEB_HEADERS_JSON": "",
                "CLAUDE_WEB_HEADERS_FILE": "",
                "CLAUDE_WEB_FETCH_MODE": "http",
            },
            clear=False,
        ):
            with pytest.raises(RuntimeError, match="Cloudflare challenge"):
                scrape_claude_usage_web()

    @patch("src.pty_scraper._run_claude_usage_browser_helper")
    def test_browser_fetch_preserves_usage_when_credit_endpoints_are_unavailable(self, mock_helper):
        mock_helper.return_value = {
            "usage": {
                "five_hour": {"utilization": 24, "resets_at": "2026-07-25T21:30:00Z"},
                "seven_day": {"utilization": 17, "resets_at": "2026-07-28T01:00:00Z"},
                "limits": [{
                    "kind": "weekly_scoped",
                    "group": "weekly",
                    "percent": 9,
                    "resets_at": "2026-07-28T01:00:00Z",
                    "scope": {"model": {"display_name": "Fable"}},
                }],
            },
            "overage": {"data": None, "error": "not available"},
            "prepaid": {"data": None, "error": "not available"},
        }

        with (
            patch("src.pty_scraper.load_provider_credential", return_value=None),
            patch.dict(
                os.environ,
                {
                    "CLAUDE_WEB_COOKIE": "sessionKey=test-session; lastActiveOrg=org-123",
                    "CLAUDE_WEB_ORG_ID": "",
                    "CLAUDE_WEB_HEADERS_JSON": "",
                    "CLAUDE_WEB_HEADERS_FILE": "",
                    "CLAUDE_WEB_FETCH_MODE": "browser",
                },
                clear=False,
            ),
        ):
            result = scrape_claude_usage_web()

        assert result["session_pct"] == 24
        assert result["weekly_pct"] == 17
        assert result["weekly_fable_pct"] == 9
        assert "credit_pools" not in result


class TestScrapeCodexUsage:
    @pytest.fixture(autouse=True)
    def _codex_bin_present(self):
        # scrape_codex_usage() bails out early if the codex binary is not
        # installed; stub the lookup so these tests don't depend on the host.
        with patch("src.pty_scraper.shutil.which", return_value="/usr/local/bin/codex"):
            yield

    @patch("src.pty_scraper.subprocess.Popen")
    def test_parses_rate_limits(self, mock_popen):
        mock_proc = MagicMock()
        mock_popen.return_value = mock_proc

        responses = [
            json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"capabilities": {}}}) + "\n",
            json.dumps({
                "jsonrpc": "2.0", "id": 2,
                "result": {
                    "rateLimits": {
                        "primary": {"usedPercent": 30, "resetsAt": 1744300000},
                        "secondary": {"usedPercent": 20, "resetsAt": 1744400000},
                    }
                }
            }) + "\n",
        ]
        mock_proc.stdout.readline = MagicMock(side_effect=responses + [""])

        with patch("src.pty_scraper.CODEX_RECV_TIMEOUT", 2):
            result = scrape_codex_usage()

        assert result is not None
        assert result["session_remaining_pct"] == 70
        assert result["weekly_remaining_pct"] == 80

    @patch("src.pty_scraper.subprocess.Popen")
    def test_does_not_assume_unlabeled_tertiary_is_code_review(self, mock_popen):
        mock_proc = MagicMock()
        mock_popen.return_value = mock_proc

        responses = [
            json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"capabilities": {}}}) + "\n",
            json.dumps({
                "jsonrpc": "2.0", "id": 2,
                "result": {
                    "rateLimits": {
                        "primary": {"usedPercent": 30, "resetsAt": 1744300000},
                        "secondary": {"usedPercent": 20, "resetsAt": 1744400000},
                        "tertiary": {"usedPercent": 40, "resetsAt": 1744500000},
                    }
                }
            }) + "\n",
        ]
        mock_proc.stdout.readline = MagicMock(side_effect=responses + [""])

        with patch("src.pty_scraper.CODEX_RECV_TIMEOUT", 2):
            result = scrape_codex_usage()

        assert result is not None
        assert result["session_remaining_pct"] == 70
        assert result["weekly_remaining_pct"] == 80
        assert "code_review_remaining_pct" not in result
        assert any(limit["id"] == "codex-tertiary-tertiary" for limit in result["limits"])

    def test_parses_free_monthly_account_limit(self):
        result = _parse_codex_rate_limits_payload({
            "rateLimitsByLimitId": {
                "codex": {
                    "limitId": "codex",
                    "planType": "free",
                    "primary": {
                        "usedPercent": 84,
                        "windowDurationMins": 43200,
                        "resetsAt": 1785896760,
                    },
                },
            },
        })

        assert result["raw_plan"] == "free"
        assert result["account_remaining_pct"] == 16
        assert "session_remaining_pct" not in result
        assert "weekly_remaining_pct" not in result
        assert result["limits"][0]["window_kind"] == "monthly"

    def test_parses_pro_dynamic_limits_and_credit_pools(self):
        result = _parse_codex_rate_limits_payload({
            "rateLimitsByLimitId": {
                "codex": {
                    "limitId": "codex",
                    "planType": "pro-20x",
                    "credits": {
                        "balance": "0",
                        "hasCredits": False,
                        "unlimited": False,
                        "spendControlReached": True,
                    },
                    "primary": {"usedPercent": 30, "windowDurationMins": 300},
                    "secondary": {"usedPercent": 61, "windowDurationMins": 10080},
                },
                "codex_spark": {
                    "limitId": "codex_spark",
                    "limitName": "GPT-5.3-Codex-Spark",
                    "planType": "pro-20x",
                    "primary": {"usedPercent": 0, "windowDurationMins": 10080},
                },
            },
            "rateLimitResetCredits": {
                "availableCount": 2,
                "credits": [
                    {"expiresAt": 1786558597, "status": "available"},
                    {"expiresAt": 1789150597, "status": "available"},
                ],
            },
        })

        assert result["session_remaining_pct"] == 70
        assert result["weekly_remaining_pct"] == 39
        assert result["weekly_spark_remaining_pct"] == 100
        assert result["credits_remaining"] == 0
        assert result["rate_limit_reset_credits"] == 2
        assert {pool["kind"] for pool in result["credit_pools"]} == {"purchased", "reset"}
        purchased = next(pool for pool in result["credit_pools"] if pool["kind"] == "purchased")
        banked = next(pool for pool in result["credit_pools"] if pool["kind"] == "reset")
        assert purchased["enabled"] is False
        assert purchased["unlimited"] is False
        assert purchased["spend_control_reached"] is True
        assert banked["next_expiry_at"] == 1786558597

    @patch("src.pty_scraper.subprocess.Popen")
    def test_parses_model_sublimit_buckets(self, mock_popen):
        mock_proc = MagicMock()
        mock_popen.return_value = mock_proc

        responses = [
            json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"capabilities": {}}}) + "\n",
            json.dumps({
                "jsonrpc": "2.0", "id": 2,
                "result": {
                    "rateLimits": {
                        "primary": {"usedPercent": 30, "resetsAt": 1744300000},
                        "secondary": {"usedPercent": 20, "resetsAt": 1744400000},
                        "gpt-5.4": {"usedPercent": 15, "name": "GPT-5.4 weekly"},
                        "codex-spark": {"usedPercent": 8, "label": "GPT-5.3 Codex Spark"},
                    }
                }
            }) + "\n",
        ]
        mock_proc.stdout.readline = MagicMock(side_effect=responses + [""])

        with patch("src.pty_scraper.CODEX_RECV_TIMEOUT", 2):
            result = scrape_codex_usage()

        assert result is not None
        assert result["session_remaining_pct"] == 70
        assert result["weekly_remaining_pct"] == 80
        assert result["weekly_gpt54_remaining_pct"] == 85
        assert result["weekly_spark_remaining_pct"] == 92

    @patch("src.pty_scraper.subprocess.Popen")
    def test_omits_model_sublimits_when_absent(self, mock_popen):
        mock_proc = MagicMock()
        mock_popen.return_value = mock_proc

        responses = [
            json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"capabilities": {}}}) + "\n",
            json.dumps({
                "jsonrpc": "2.0", "id": 2,
                "result": {
                    "rateLimits": {
                        "primary": {"usedPercent": 30, "resetsAt": 1744300000},
                        "secondary": {"usedPercent": 20, "resetsAt": 1744400000},
                    }
                }
            }) + "\n",
        ]
        mock_proc.stdout.readline = MagicMock(side_effect=responses + [""])

        with patch("src.pty_scraper.CODEX_RECV_TIMEOUT", 2):
            result = scrape_codex_usage()

        assert result is not None
        assert "weekly_gpt54_remaining_pct" not in result
        assert "weekly_spark_remaining_pct" not in result

    @patch("src.pty_scraper.subprocess.Popen")
    def test_no_response(self, mock_popen):
        mock_proc = MagicMock()
        mock_popen.return_value = mock_proc
        mock_proc.stdout.readline = MagicMock(return_value="")

        with patch("src.pty_scraper.CODEX_RECV_TIMEOUT", 0.2):
            result = scrape_codex_usage()
        assert result is None
        mock_proc.terminate.assert_called_once()

    @patch("src.pty_scraper.subprocess.Popen")
    def test_never_uses_select_on_the_pipe(self, mock_popen):
        # select() accepts only sockets on Windows; reading the app-server pipe through it
        # raised there, so Codex quota was never read on Windows.
        mock_proc = MagicMock()
        mock_popen.return_value = mock_proc
        mock_proc.stdout.readline = MagicMock(side_effect=[
            json.dumps({"method": "remoteControl/status/changed", "params": {}}) + "\n",
            "not json\n",
            json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}) + "\n",
            json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"rateLimits": {
                "primary": {"usedPercent": 30, "resetsAt": 1744300000},
                "secondary": {"usedPercent": 20, "resetsAt": 1744400000},
            }}}) + "\n",
            "",
        ])
        import select

        with patch.object(select, "select", side_effect=AssertionError("select() used")), \
                patch("src.pty_scraper.CODEX_RECV_TIMEOUT", 2):
            result = scrape_codex_usage()
        assert result is not None
        assert result["session_remaining_pct"] == 70
        mock_proc.terminate.assert_called_once()


class TestScrapeCodexAnalytics:
    def test_helper_script_is_committed(self):
        assert _codex_analytics_helper_script().exists()

    def test_config_detected_from_cookie(self):
        with patch.dict(
            os.environ,
            {
                "CODEX_WEB_COOKIE": "__Secure-next-auth.session-token.0=test-token",
                "CODEX_WEB_COOKIE_FILE": "",
                "CODEX_WEB_HEADERS_JSON": "",
                "CODEX_WEB_HEADERS_FILE": "",
                "CODEX_WEB_ANALYTICS_URL": "https://chatgpt.com/codex/cloud/settings/analytics",
            },
            clear=False,
        ):
            assert codex_web_analytics_configured() is True

    @patch("src.pty_scraper.subprocess.run")
    def test_fetches_codex_analytics_bundle(self, mock_run):
        helper_payload = {
            "window_days": 7,
            "group_by": "day",
            "date_range": {"start_date": "2026-04-03", "end_date": "2026-04-09"},
            "include_emails": False,
            "daily_workspace_usage_counts": {
                "data": [
                    {
                        "date": "2026-04-09",
                        "totals": {"users": 12, "threads": 20, "turns": 40, "credits": 8.5},
                        "clients": [
                            {"client_id": "CODEX_WEB", "users": 7, "threads": 12, "turns": 21, "credits": 4.5},
                            {"client_id": "CODEX_CLI", "users": 5, "threads": 8, "turns": 19, "credits": 4.0},
                        ],
                    }
                ]
            },
            "daily_sessions_messages_counts": {
                "data": [
                    {
                        "date": "2026-04-09",
                        "n_new_sessions_total": 10,
                        "n_user_messages_total": 30,
                        "n_users_used_codex": 8,
                        "n_tasks_web": 3,
                        "n_code_reviews_web": 2,
                        "credit_total": 6.25,
                    }
                ]
            },
            "daily_code_review_metrics": {
                "data": [
                    {
                        "date": "2026-04-09",
                        "n_reviews": 4,
                        "n_comments": 9,
                        "n_comments_p0": 1,
                        "n_comments_p1": 3,
                        "n_comments_p2": 5,
                    }
                ]
            },
        }
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps(helper_payload),
            stderr="",
        )

        with patch.dict(
            os.environ,
            {
                "CODEX_WEB_COOKIE": "__Secure-next-auth.session-token.0=test-token",
                "CODEX_WEB_COOKIE_FILE": "",
                "CODEX_WEB_HEADERS_JSON": "",
                "CODEX_WEB_HEADERS_FILE": "",
                "CODEX_WEB_ANALYTICS_URL": "https://chatgpt.com/codex/cloud/settings/analytics",
                "CODEX_ANALYTICS_WINDOW_DAYS": "7",
                "CODEX_ANALYTICS_GROUP_BY": "day",
            },
            clear=False,
        ):
            result = scrape_codex_analytics()

        assert result["daily_workspace_usage_counts"]["data"][0]["totals"]["users"] == 12
        assert result["summary"]["workspace"]["avg_daily_users"] == 12.0
        assert result["summary"]["sessions_messages"]["avg_daily_sessions"] == 10.0
        assert result["summary"]["code_review"]["avg_daily_reviews"] == 4.0
        assert result["date_range"]["start_date"] == "2026-04-03"

        helper_call = mock_run.call_args
        helper_input = json.loads(helper_call.kwargs["input"])
        assert helper_input["analytics_page_url"] == "https://chatgpt.com/codex/cloud/settings/analytics"
        assert helper_input["group_by"] == "day"
        assert helper_input["window_days"] == 7

    @patch("src.pty_scraper.subprocess.run")
    def test_helper_failure_raises_runtime_error(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="Unauthorized")

        with patch.dict(
            os.environ,
            {
                "CODEX_WEB_COOKIE": "__Secure-next-auth.session-token.0=test-token",
                "CODEX_WEB_COOKIE_FILE": "",
                "CODEX_WEB_HEADERS_JSON": "",
                "CODEX_WEB_HEADERS_FILE": "",
                "CODEX_WEB_ANALYTICS_URL": "https://chatgpt.com/codex/cloud/settings/analytics",
            },
            clear=False,
        ):
            with pytest.raises(RuntimeError, match="Unauthorized"):
                scrape_codex_analytics()


class TestCursorAuthToken:
    def test_no_db_file(self):
        with patch.object(Path, "exists", return_value=False):
            assert _cursor_auth_token() is None

    def test_reads_token(self, tmp_path):
        import sqlite3
        db_file = tmp_path / "state.vscdb"
        conn = sqlite3.connect(db_file)
        conn.execute("CREATE TABLE ItemTable(key TEXT, value TEXT)")
        conn.execute("INSERT INTO ItemTable VALUES('cursorAuth/accessToken', 'test-token-123')")
        conn.commit()
        conn.close()

        with patch("src.pty_scraper._CURSOR_STATE_DB", db_file):
            token = _cursor_auth_token()
        assert token == "test-token-123"

    def test_no_token_row(self, tmp_path):
        import sqlite3
        db_file = tmp_path / "state.vscdb"
        conn = sqlite3.connect(db_file)
        conn.execute("CREATE TABLE ItemTable(key TEXT, value TEXT)")
        conn.commit()
        conn.close()

        with patch("src.pty_scraper._CURSOR_STATE_DB", db_file):
            assert _cursor_auth_token() is None


class TestScrapeCursorUsage:
    def test_parses_cursor_agent_free_limit_text(self):
        text = """
        Error: You've hit your usage limit
        fallbackModel:
        spendLimitHit: false
        chatMessage: *You've hit your free requests limit. [Upgrade to
        Pro](https://www.cursor.com/api/auth/checkoutDeepControl?tier=pro) for more usage.
        Your usage limits will reset when your monthly cycle ends on 12/31/2026.*
        spendLimits: [50,100,200]
        """

        result = _parse_cursor_agent_limit_text(text)

        assert result is not None
        assert result["limit_hit"] is True
        assert result["at_limit"] is True
        assert result["limit_kind"] == "free_requests"
        assert result["limit_message"] == "You've hit your free requests limit."
        assert result["plan"] == "free"
        assert result["total_requests"] == 50
        assert result["max_requests"] == 50
        assert result["remaining_requests"] == 0
        assert result["reset_at"] == "2026-12-31"
        assert result["spend_limit_hit"] is False
        assert result["spend_limits"] == [50, 100, 200]
        assert "fallback_model" not in result

    @patch("src.pty_scraper._cursor_auth_token", return_value=None)
    def test_no_token_returns_configured_cursor_limit_state(self, _mock_token, tmp_path):
        with patch.dict(
            os.environ,
            {
                "CURSOR_AGENT_LIMIT_TEXT": "You've hit your free requests limit. monthly cycle ends on 12/31/2026",
                "CURSOR_AGENT_LIMIT_FILE": "",
            },
            clear=False,
        ), patch("src.pty_scraper._CURSOR_LIMIT_STATE_FILE", tmp_path / "missing-status.txt"):
            result = scrape_cursor_usage()

        assert result is not None
        assert result["at_limit"] is True
        assert result["total_requests"] == 50
        assert result["remaining_requests"] == 0

    @patch("src.pty_scraper._cursor_auth_token", return_value=None)
    def test_no_token_reads_default_cursor_limit_file(self, _mock_token, tmp_path):
        limit_file = tmp_path / "cursor-agent-status.txt"
        limit_file.write_text("You've hit your free requests limit. monthly cycle ends on 12/31/2026")

        with patch.dict(
            os.environ,
            {"CURSOR_AGENT_LIMIT_TEXT": "", "CURSOR_AGENT_LIMIT_FILE": ""},
            clear=False,
        ), patch("src.pty_scraper._CURSOR_LIMIT_STATE_FILE", limit_file):
            result = scrape_cursor_usage()

        assert result is not None
        assert result["at_limit"] is True
        assert result["reset_at"] == "2026-12-31"

    @patch("src.pty_scraper._cursor_auth_token", return_value=None)
    def test_ignores_default_cursor_limit_file_after_reset(self, _mock_token, tmp_path):
        limit_file = tmp_path / "cursor-agent-status.txt"
        limit_file.write_text("You've hit your free requests limit. monthly cycle ends on 1/1/2020")

        with patch.dict(
            os.environ,
            {"CURSOR_AGENT_LIMIT_TEXT": "", "CURSOR_AGENT_LIMIT_FILE": ""},
            clear=False,
        ), patch("src.pty_scraper._CURSOR_LIMIT_STATE_FILE", limit_file):
            assert scrape_cursor_usage() is None

    def test_stale_free_limit_does_not_override_paid_plan(self):
        result = _merge_cursor_limit_state(
            {"plan": "pro", "total_requests": 10, "max_requests": 500},
            {
                "plan": "free",
                "limit_hit": True,
                "at_limit": True,
                "limit_kind": "free_requests",
                "total_requests": 50,
                "max_requests": 50,
            },
        )

        assert result == {"plan": "pro", "total_requests": 10, "max_requests": 500}

    @patch("src.pty_scraper.urllib.request.urlopen")
    @patch("src.pty_scraper._cursor_auth_token", return_value="test-token")
    def test_basic(self, mock_token, mock_urlopen, tmp_path):
        usage_resp = MagicMock()
        usage_resp.read.return_value = json.dumps({
            "gpt-4": {"numRequestsTotal": 10, "numTokens": 5000, "maxRequestUsage": 100},
            "startOfMonth": "2026-04-01",
        }).encode()
        usage_resp.__enter__ = MagicMock(return_value=usage_resp)
        usage_resp.__exit__ = MagicMock(return_value=False)

        profile_resp = MagicMock()
        profile_resp.read.return_value = json.dumps({
            "membershipType": "pro", "trialEligible": False,
        }).encode()
        profile_resp.__enter__ = MagicMock(return_value=profile_resp)
        profile_resp.__exit__ = MagicMock(return_value=False)

        mock_urlopen.side_effect = [usage_resp, profile_resp]
        with patch.dict(
            os.environ,
            {"CURSOR_AGENT_LIMIT_TEXT": "", "CURSOR_AGENT_LIMIT_FILE": ""},
            clear=False,
        ), patch("src.pty_scraper._CURSOR_LIMIT_STATE_FILE", tmp_path / "missing-status.txt"):
            result = scrape_cursor_usage()
        assert result is not None
        assert result["total_requests"] == 10
        assert result["plan"] == "pro"

    @patch("src.pty_scraper._cursor_auth_token", return_value=None)
    def test_no_token(self, mock, tmp_path):
        with patch.dict(
            os.environ,
            {"CURSOR_AGENT_LIMIT_TEXT": "", "CURSOR_AGENT_LIMIT_FILE": ""},
            clear=False,
        ), patch("src.pty_scraper._CURSOR_LIMIT_STATE_FILE", tmp_path / "missing-status.txt"):
            assert scrape_cursor_usage() is None
