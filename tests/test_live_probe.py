"""Probe response classification without login, configuration writes or network."""
import pytest
import live_probe as probe


@pytest.mark.parametrize("response,expected", [
    ({"code": 500, "data": {"runFaceStudentStatus": "Y"}}, 4),
    ({"code": 200, "data": None}, 4),
    ({"code": 200, "data": []}, 4),
    ({"code": 200, "data": {"runFaceStudentStatus": "N1"}}, 5),
    ({"code": 200, "data": {"runFaceStudentStatus": "unknown"}}, 5),
    ({"code": 200, "data": {"runFaceStudentStatus": "Y"}}, 0),
])
def test_probe_does_not_report_failed_or_unknown_response_as_go(monkeypatch, response, expected):
    calls = []
    class Client:
        def post_json(self, router, *args, **kwargs):
            calls.append(router)
            return response
    monkeypatch.setattr(probe.M, "set_args", lambda *a: None)
    monkeypatch.setattr(probe.M, "my_host", "offline.invalid")
    monkeypatch.setattr(probe.Login, "main", lambda *a: ("fixture",))
    monkeypatch.setattr(probe.M, "apply_login_result", lambda *a, **kw: None)
    monkeypatch.setattr(probe.M, "default_client", lambda: Client())
    assert probe.run("A") == expected
    assert calls == ["/run/getRlStatus"]
