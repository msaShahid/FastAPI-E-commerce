"""What an administrator types about a shipment is checked as if hostile (the URL becomes a link on the customer's order page)."""

import pytest

from app.modules.shipments import validation as v


@pytest.mark.parametrize("url", [
    "https://track.example-courier.com/p/123456",
    "https://www.bluedart.com/tracking?awb=1234567890",
    "https://tracking.example.com/a/b?c=d&e=f#top",
    "https://example.com:443/track",
    "HTTPS://Courier.CO.IN/Track?id=1",
    "https://a-b.example.in/%E0%A4%B8",
])
def test_good_tracking_urls_are_kept_as_typed(url):
    assert v.tracking_url(f"  {url}  ") == url


@pytest.mark.parametrize("url", [
    "http://courier.com/track", "javascript:alert(1)", "data:text/html,<script>1</script>", "ftp://courier.com/x", "//courier.com/x",
    "https://user:pass@courier.com/x", "https://courier.com@evil.example/x", "https://localhost/x", "https://127.0.0.1/x",
    "https://[::1]/x", "https://10.0.0.5/x", "https://192.168.1.10/track", "https://intranet/x", "https://printer.local/x",
    "https://host.internal/x", "https://courier.com:8443/x", "https://cour ier.com/x", "https://courier.com/a b",
    "https://courier.com/x\r\nSet-Cookie: a=b", "https://courier.com/\x00", "https://exämple.com/x", "https://courier.com/ü",
    "https://-bad-.com/x", "https://courier.c/x", "https://courier..com/x", "https:///x", "", "   ", "https://" + "a" * 2050 + ".com",
])
def test_unsafe_or_malformed_tracking_urls_are_refused(url):
    with pytest.raises(ValueError):
        v.tracking_url(url)


@pytest.mark.parametrize("name", ["Delhivery", "Blue Dart Express", "India Post (Speed Post)", "DTDC", "Ecom Express", "R&L / Partner-1", "  Xpressbees  "])
def test_carrier_names(name):
    assert v.carrier_name(name) == " ".join(name.split())


@pytest.mark.parametrize("name", ["A", "", "   ", "<script>x</script>", "line\nbreak", "tab\tname", "x" * 61, "Évian", "name;drop"])
def test_bad_carrier_names_are_refused(name):
    with pytest.raises(ValueError):
        v.carrier_name(name)


@pytest.mark.parametrize("number", ["DEL1234567890", "1Z999AA10123456784", "AB-123/456.7", "abc", "  EE123456789IN  "])
def test_tracking_numbers(number):
    assert v.tracking_number(number) == number.strip()


@pytest.mark.parametrize("number", ["12", "has space", "x" * 65, "ab\n123", "@@@@", "", "-abc", "a b c"])
def test_bad_tracking_numbers_are_refused(number):
    with pytest.raises(ValueError):
        v.tracking_number(number)
