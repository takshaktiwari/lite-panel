# Legacy setup scripts

These are the hand-run bash scripts this repo started as, before it became
lite-panel. **Nothing in the panel executes them** — the panel is not a wrapper
around these, and none of this code ships.

They are kept here for one reason: several of them encode operational
knowledge that has not been carried across yet. Deleting them before that
happens would lose it.

## Still to extract

| Script | What is still needed from it | Destination |
|---|---|---|
| `main.sh` | PHP-FPM tuning tiers (`pm` mode and `pm.max_children` by RAM) and the php.ini tiers (`memory_limit`, `opcache.memory_consumption`) | `services/tuning.py` |
| `mariadb.sh` | InnoDB sizing formula — buffer pool at 60% of half of RAM, temp tables 5%, log file 15%, connection tiers, and the floors under each | `services/tuning.py` |
| `ftp.sh` | vsftpd passive-mode setup: port range 30000–30100, `pasv_address` from the public IP, per-user config dir, `allow_writeable_chroot` | `providers/vsftpd.py` |
| `domain.sh` | The working nginx vhost shape: www→non-www redirect, static-asset caching block, `try_files` for front controllers, php-fpm socket wiring | `assets/templates/vhost.conf.j2` |
| `swap.sh` | Swap sizing tiers by RAM (1GB under 512MB RAM, else 2GB capped) | phase 2 |
| `backup.sh` | Per-site zip + per-database `mysqldump` layout, and its output/report structure | phase 2 |

## Safe to delete now

- `domain-addon.sh` — a variant of `domain.sh`, nothing unique in it.
- `email.sh` — mail server setup, unfinished and broken (see commit `858b100`).
  Email is explicitly out of scope for lite-panel v1.

## What is *not* worth carrying over

The patterns these scripts use are the ones the panel exists to replace, and
they should not be imitated when porting the maths above:

- Values interpolated straight into heredocs — `CREATE DATABASE \`${DB_NAME}\``,
  `server_name $DOMAIN` — with no validation. Fine when only you type them at a
  root prompt; a serious hole behind a web form.
- `sed`-patching vendor config files in place, which cannot be re-run cleanly
  or undone. The panel writes only its own drop-in files instead.
- Choices frozen at run time (one PHP version, one hardcoded extension list).

Once the table above is empty, delete this directory.
