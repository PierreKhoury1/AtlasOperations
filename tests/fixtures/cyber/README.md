# Cyber desk test fixtures

Excerpts from Security Repo by Mike Sconzo (https://www.secrepo.com), licensed CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/). MACCDC 2012 captures: National CyberWatch Mid-Atlantic CCDC (via SecRepo). Lines are copied unchanged; only selected.

- `auth_sample.log`: the first 45 lines of SecRepo `auth.log` (sshd and CRON, Nov 30, no year in the lines) plus the first two lines each of `Too many authentication failures`, `Bad protocol version identification`, `reverse mapping checking`, `Corrupted MAC on input` and one more `fatal: Read from socket failed`, in file order (54 lines).
- `auth_campaign.log.gz`: every `Invalid user` line from 61.197.203.243, 220.99.93.50, 218.25.17.234 and 188.87.35.25 (4 x 409 attempts with the same username sequence) plus the first `Invalid user` line of 30 other sources, in file order (1,666 lines, gzip).
- `zeek_notice_sample.log`: MACCDC 2012 Zeek `notice.log` (headerless, 26 columns): the first 12 lines plus every non-SSL notice whose `src` column is 192.168.202.140 or 192.168.202.110, in file order (46 lines).
- `zeek_ssh_sample.log`: MACCDC 2012 Zeek `ssh.log` (headerless, 15 columns): every line of 192.168.202.110 in the hour up to and including its "success" to 192.168.28.253 at 1331910012.66, plus the first 30 failures of 192.168.202.140, in file order (136 lines).
- `snort_fast_sample.log`: MACCDC 2012 Snort fast alerts, file `alert.fast.maccdc2012_00016.pcap` from `maccdc2012_fast_alert.7z`: the first 20 lines, 15 + 15 Meterpreter alerts between 192.168.202.140 and 192.168.25.103 (both directions), 5 + 5 with 192.168.21.103, 3 ICMP, 2 IPv4 UDP, 1 bracket-less IPv6 UDP and 3 IPV6-ICMP alerts, in file order (69 lines).
- `web_sample.log`: SecRepo Apache combined access logs (January 2017): 40 ordinary lines and every `/wp-login.php` line of `web_01`, every `/wp-login.php` line of `web_03`, the `/wp-admin/` line of `web_04` and the two `/xmlrpc.php` lines of `web_05` and `web_06` (195 lines; `web_02` is left out to keep the file short).

Synthetic lines for formats these datasets do not contain (`Accepted` / `Failed password`, RFC 3339 syslog, Suricata years, headered Zeek, escaped quotes in a user agent) live inside `tests/test_cyber.py`, each labelled `# synthetic`.
