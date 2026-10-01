from traffic import advance, host, render, sample

# `ss -tinpH state established` on the Pi, 2026-09-30, plus a session from
# the PC (192.168.1.131).
SS = """\
0      0                   192.168.1.253:33899            192.168.1.253:21116 users:(("rustdesk",pid=2436348,fd=20))
\t cubic wscale:10,10 rto:200 rtt:0.055/0.027 bytes_acked:1 segs_out:2 segs_in:1 lastsnd:7016
0      0          [::ffff:192.168.1.253]:21116   [::ffff:192.168.1.253]:33899 users:(("hbbs",pid=78481,fd=12))
\t cubic wscale:10,10 rto:200 rtt:0.033/0.016 segs_in:2 lastsnd:7016
0      0                   192.168.1.253:41234            192.168.1.131:50521 users:(("rustdesk",pid=2436348,fd=31))
\t cubic rto:204 bytes_sent:5200 bytes_acked:5000 bytes_received:700 segs_out:9
0      0                   192.168.1.253:22               192.168.1.131:50000 users:(("sshd",pid=1,fd=4))
\t cubic rto:204 bytes_acked:999999 bytes_received:999999
"""
MINE = {"192.168.1.253", "127.0.0.1", "::1"}


def test_host_strips_brackets_and_mapped_prefix():
    assert host("[::ffff:192.168.1.253]:21116") == "192.168.1.253"
    assert host("192.168.1.131:50521") == "192.168.1.131"


def test_only_rustdesk_sockets_to_other_machines():
    found = sample(SS, MINE)
    # Not the Pi's client talking to its own ID server, and not sshd.
    assert found == {("rustdesk", "192.168.1.253:41234", "192.168.1.131:50521"): (5000, 700)}


def test_totals_grow_by_each_sockets_growth():
    totals = [0, 0]
    key = ("rustdesk", "a:1", "b:2")
    advance(totals, {}, {key: (5000, 700)})  # a new socket adds all it has moved
    advance(totals, {key: (5000, 700)}, {key: (8000, 900)})
    assert totals == [8000, 900]
    advance(totals, {key: (8000, 900)}, {key: (100, 10)})  # same address pair, new socket
    assert totals == [8100, 910]


def test_render_is_prometheus_text():
    text = render(8100, 910)
    assert "# TYPE rustdesk_sent_bytes_total counter\nrustdesk_sent_bytes_total 8100\n" in text
    assert text.endswith("rustdesk_received_bytes_total 910\n")
