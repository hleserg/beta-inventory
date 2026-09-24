# Setup: server, address, phone

## 1. Install

On any always-on Linux box in your home network (a mini PC, a NAS, a Raspberry
Pi) with [Docker](https://docs.docker.com/engine/install/):

```sh
git clone https://github.com/hleserg/beta-inventory && cd beta-inventory && ./install.sh
```

It asks two things, the site address and the port, writes them to `.env` and
starts the container. Update later with `git pull && ./install.sh`. Every other
setting is in `.env`, described in `.env.example`.

Slow PyPI during the build? Set `PIP_INDEX_URL` in `.env` to a mirror.

## 2. Name or IP

The address goes into every label: the QR code and the NFC tag. Printed labels
can't be changed, so pick before you print.

| | IP (`http://192.168.1.51`) | Name (`http://inv.lan`) |
|---|---|---|
| Works | right away | after a DNS record in the router |
| Server gets another IP | every label is dead | change one DNS record |

Either way, give the server a fixed IP: a DHCP reservation (static lease) in the
router.

Port 80 keeps the address short: `http://inv.lan`. Any other port becomes part
of it (`http://inv.lan:8000`), in labels too.

## 3. A name in your router's DNS

The router answers DNS for your devices, so the name goes there: `inv.lan` →
the server's IP. Where to find it:

- **Keenetic:** web CLI at `http://<router>/a`:
  `ip host inv.lan 192.168.1.51`, then `system configuration save`.
- **OpenWrt:** Network → DHCP and DNS → Hostnames, or:
  ```sh
  uci add dhcp domain && uci set dhcp.@domain[-1].name=inv.lan && uci set dhcp.@domain[-1].ip=192.168.1.51
  uci commit dhcp && service dnsmasq restart
  ```
- **Pi-hole:** Settings → Local DNS Records.
- **Other routers:** look for "DNS records", "Local DNS", "Static DNS" or "hosts".

Avoid `.local`: that is mDNS, and older Android phones can't resolve it.

Check from the phone: open `http://inv.lan`. It doesn't open while the PC finds
it? On Android, Settings → Private DNS must be *Off* or *Automatic*: a fixed
provider there skips the router and its names.

## 4. Android: app and NFC tags

Open `<site address>/phone` in **Chrome**. The page shows the steps with your
address filled in:

1. Turn NFC on.
2. In `chrome://flags/#unsafely-treat-insecure-origin-as-secure` add the site
   address, port included, choose *Enabled*, tap *Relaunch*. Chrome lets only
   "secure" sites write NFC tags, and a home site has no HTTPS.
3. Chrome menu ⋮ → *Add to Home screen* (or *Install app*). Tags then open in
   the app.

Write a tag: box page → «Настроить, наклейка» (setup, label) → «Записать на
метку» (write to tag), hold the tag to the back of the phone. NTAG213 tags are
enough.

Samsung Internet and Firefox can't write tags; they open them fine.

## 5. iPhone

iOS doesn't let websites write NFC tags. The free
[NFC Tools](https://apps.apple.com/app/nfc-tools/id1252962749) app does: copy
«Ссылка для NFC-метки» (NFC link) from the box page, then in NFC Tools:
Write → Add a record → URL/URI → paste → Write. iPhone XS and newer read the tags
without any app.

App on the home screen: Safari → Share → *Add to Home Screen*.

## 6. Labels

Box page → «Настроить, наклейка» gives a PNG with the QR code and the box ID,
sized for a label printer (`LABEL_W_MM`, `LABEL_H_MM`, `LABEL_DPI` in `.env`).
Print it from the printer's app. The QR carries the address in capitals: that
makes the code smaller, and the site accepts either case.

## Changing the address later

Edit `PUBLIC_BASE_URL` in `.env`, then `docker compose up -d`. Labels already
printed keep the old address: keep it working (old name in DNS, old IP) or
reprint them.
