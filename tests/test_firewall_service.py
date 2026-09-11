"""Tests for UFW firewall service and rule parser."""

import unittest
from unittest.mock import patch, MagicMock

from app.services import firewall as fw_service
from app.validators import ValidationError


SAMPLE_UFW_VERBOSE = """Status: active
Logging: on (low)
Default: deny (incoming), allow (outgoing), disabled (routed)
New profiles: skip
"""

SAMPLE_UFW_NUMBERED = """Status: active

     To                         Action      From
     --                         ------      ----
[ 1] 22/tcp                     ALLOW IN    Anywhere                   # SSH
[ 2] 80/tcp                     ALLOW IN    Anywhere                   # Web
[ 3] 443/tcp                    ALLOW IN    Anywhere                   # SSL
[ 4] 3306/tcp                   ALLOW IN    192.168.1.50               # Remote DB
[ 5] 22/tcp (v6)                ALLOW IN    Anywhere (v6)              # SSH
"""


class TestFirewallService(unittest.TestCase):

    def test_parse_numbered_rules(self):
        rules = fw_service.parse_numbered_rules(SAMPLE_UFW_NUMBERED)
        self.assertEqual(len(rules), 5)
        self.assertEqual(rules[0].number, 1)
        self.assertEqual(rules[0].to_port, "22/tcp")
        self.assertEqual(rules[0].action, "ALLOW")
        self.assertEqual(rules[0].from_ip, "Anywhere")
        self.assertEqual(rules[0].comment, "SSH")

        self.assertEqual(rules[3].number, 4)
        self.assertEqual(rules[3].to_port, "3306/tcp")
        self.assertEqual(rules[3].from_ip, "192.168.1.50")
        self.assertEqual(rules[3].comment, "Remote DB")

    def test_port_validation(self):
        self.assertEqual(fw_service.validate_port_specification("80"), "80")
        self.assertEqual(fw_service.validate_port_specification("40000:40100"), "40000:40100")

        with self.assertRaises(ValidationError):
            fw_service.validate_port_specification("abc")
        with self.assertRaises(ValidationError):
            fw_service.validate_port_specification("70000")
        with self.assertRaises(ValidationError):
            fw_service.validate_port_specification("22; rm -rf /")

    def test_ip_validation(self):
        self.assertEqual(fw_service.validate_ip_specification("any"), "any")
        self.assertEqual(fw_service.validate_ip_specification("Anywhere"), "any")
        self.assertEqual(fw_service.validate_ip_specification("1.2.3.4"), "1.2.3.4")
        self.assertEqual(fw_service.validate_ip_specification("10.0.0.0/24"), "10.0.0.0/24")

        with self.assertRaises(ValidationError):
            fw_service.validate_ip_specification("bad_ip_address")

    @patch("app.services.firewall.which", return_value="/usr/sbin/ufw")
    @patch("app.services.firewall.run")
    def test_get_status(self, mock_run, mock_which):
        def fake_run(cmd, **kwargs):
            res = MagicMock()
            if "verbose" in cmd:
                res.stdout = SAMPLE_UFW_VERBOSE
            elif "numbered" in cmd:
                res.stdout = SAMPLE_UFW_NUMBERED
            return res

        mock_run.side_effect = fake_run

        status = fw_service.get_status()
        self.assertTrue(status.available)
        self.assertTrue(status.enabled)
        self.assertEqual(status.default_incoming, "deny")
        self.assertEqual(status.default_outgoing, "allow")
        self.assertEqual(len(status.rules), 5)

    @patch("app.services.firewall.which", return_value="/usr/sbin/ufw")
    @patch("app.services.firewall.run")
    def test_add_rule(self, mock_run, mock_which):
        fw_service.add_rule(port="80", proto="tcp", action="allow", from_ip="any", comment="Web")
        self.assertTrue(mock_run.called)
        cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd, ["ufw", "allow", "80/tcp", "comment", "Web"])

    @patch("app.services.firewall.which", return_value="/usr/sbin/ufw")
    @patch("app.services.firewall.run")
    def test_add_rule_with_ip(self, mock_run, mock_which):
        fw_service.add_rule(port="3306", proto="tcp", action="allow", from_ip="192.168.1.100", comment="DB")
        self.assertTrue(mock_run.called)
        cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd, ["ufw", "allow", "from", "192.168.1.100", "to", "any", "port", "3306", "proto", "tcp", "comment", "DB"])

    @patch("app.services.firewall.which", return_value="/usr/sbin/ufw")
    @patch("app.services.firewall.run")
    def test_delete_rule(self, mock_run, mock_which):
        fw_service.delete_rule(2)
        self.assertTrue(mock_run.called)
        cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd, ["ufw", "--force", "delete", "2"])

    @patch("app.services.firewall.which", return_value="/usr/sbin/ufw")
    @patch("app.services.firewall.run")
    def test_set_default_policy_deny_incoming_auto_allows_ssh(self, mock_run, mock_which):
        def fake_run(cmd, **kwargs):
            res = MagicMock()
            if "verbose" in cmd:
                res.stdout = "Status: active\nDefault: allow (incoming), allow (outgoing), disabled (routed)\n"
            elif "numbered" in cmd:
                res.stdout = "Status: active\n"
            return res

        mock_run.side_effect = fake_run

        fw_service.set_default_policy(direction="incoming", policy="deny")
        calls = [c[0][0] for c in mock_run.call_args_list]
        self.assertIn(["ufw", "allow", "22/tcp"], calls)
        self.assertIn(["ufw", "default", "deny", "incoming"], calls)

    @patch("app.services.firewall.which", return_value="/usr/sbin/ufw")
    @patch("app.services.firewall.run")
    def test_set_default_policy_deny_incoming_skips_ssh_if_present(self, mock_run, mock_which):
        def fake_run(cmd, **kwargs):
            res = MagicMock()
            if "verbose" in cmd:
                res.stdout = "Status: active\nDefault: allow (incoming), allow (outgoing), disabled (routed)\n"
            elif "numbered" in cmd:
                res.stdout = SAMPLE_UFW_NUMBERED
            return res

        mock_run.side_effect = fake_run

        fw_service.set_default_policy(direction="incoming", policy="deny")
        calls = [c[0][0] for c in mock_run.call_args_list]
        self.assertNotIn(["ufw", "allow", "22/tcp"], calls)
        self.assertIn(["ufw", "default", "deny", "incoming"], calls)

    @patch("app.services.firewall.which", return_value="/usr/sbin/ufw")
    def test_set_default_policy_invalid_direction(self, mock_which):
        with self.assertRaises(ValidationError):
            fw_service.set_default_policy(direction="sideways", policy="deny")

    @patch("app.services.firewall.which", return_value="/usr/sbin/ufw")
    def test_set_default_policy_invalid_policy(self, mock_which):
        with self.assertRaises(ValidationError):
            fw_service.set_default_policy(direction="incoming", policy="maybe")

    def test_parse_default_policy_file(self):
        import tempfile

        sample_file = """
# /etc/default/ufw
#
IPV6=yes
DEFAULT_INPUT_POLICY="DROP"
DEFAULT_OUTPUT_POLICY="ACCEPT"
DEFAULT_FORWARD_POLICY="DROP"
DEFAULT_APPLICATION_POLICY="SKIP"
"""
        with tempfile.NamedTemporaryFile("w+", delete=False) as f:
            f.write(sample_file)
            f.flush()
            temp_path = f.name

        try:
            default_in, default_out = fw_service.parse_default_policy_file(temp_path)
            self.assertEqual(default_in, "deny")
            self.assertEqual(default_out, "allow")
        finally:
            import os
            os.unlink(temp_path)

    def test_parse_default_policy_file_allow_incoming(self):
        import tempfile

        sample_file = 'DEFAULT_INPUT_POLICY="ACCEPT"\nDEFAULT_OUTPUT_POLICY="ACCEPT"\n'
        with tempfile.NamedTemporaryFile("w+", delete=False) as f:
            f.write(sample_file)
            f.flush()
            temp_path = f.name

        try:
            default_in, default_out = fw_service.parse_default_policy_file(temp_path)
            self.assertEqual(default_in, "allow")
            self.assertEqual(default_out, "allow")
        finally:
            import os
            os.unlink(temp_path)

    def test_parse_default_policy_file_missing(self):
        default_in, default_out = fw_service.parse_default_policy_file("/nonexistent/path/ufw")
        self.assertEqual(default_in, "deny")
        self.assertEqual(default_out, "allow")

    @patch("app.services.firewall.which", return_value="/usr/sbin/ufw")
    @patch("app.services.firewall.run")
    @patch("app.services.firewall.parse_default_policy_file", return_value=("allow", "allow"))
    def test_get_status_inactive_uses_default_policy_file(self, mock_parse_default, mock_run, mock_which):
        def fake_run(cmd, **kwargs):
            res = MagicMock()
            res.stdout = "Status: inactive\n"
            return res

        mock_run.side_effect = fake_run

        status = fw_service.get_status()
        self.assertFalse(status.enabled)
        self.assertEqual(status.default_incoming, "allow")
        self.assertEqual(status.default_outgoing, "allow")
        mock_parse_default.assert_called_once()

    def test_parse_user_rules_file(self):
        import tempfile
        from pathlib import Path

        sample_file = """
*filter
:ufw-user-input - [0:0]
### tuple ### allow tcp 22 0.0.0.0/0 any 0.0.0.0/0 in
-A ufw-user-input -p tcp --dport 22 -j ACCEPT -m comment --comment 'ufw-user-SSH'
### tuple ### allow tcp 80 0.0.0.0/0 any 0.0.0.0/0 in
-A ufw-user-input -p tcp --dport 80 -j ACCEPT
### tuple ### allow tcp 3306 0.0.0.0/0 any 192.168.1.50 in
-A ufw-user-input -s 192.168.1.50 -p tcp --dport 3306 -j ACCEPT -m comment --comment 'ufw-user-Remote DB'
COMMIT
"""
        with tempfile.NamedTemporaryFile("w+", delete=False) as f:
            f.write(sample_file)
            f.flush()
            temp_path = f.name

        try:
            rules = fw_service.parse_user_rules_file(temp_path)
            self.assertEqual(len(rules), 3)
            self.assertEqual(rules[0].to_port, "22/tcp")
            self.assertEqual(rules[0].action, "ALLOW")
            self.assertEqual(rules[0].from_ip, "Anywhere")
            self.assertEqual(rules[0].comment, "SSH")

            self.assertEqual(rules[2].to_port, "3306/tcp")
            self.assertEqual(rules[2].from_ip, "192.168.1.50")
            self.assertEqual(rules[2].comment, "Remote DB")
        finally:
            Path(temp_path).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
