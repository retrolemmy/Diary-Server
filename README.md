# Diary Server

Keeps the entries of all your [Diary](https://github.com/retrolemmy/Diary)
apps in one place: the classic Mac OS (68k Macs, and PowerPC Macs up to a
G4 with Mac OS 9.2.2),
[AmigaOS 3.2](https://github.com/retrolemmy/Diary-Amiga),
[MorphOS and AmigaOS with MUI](https://github.com/retrolemmy/Diary-Amiga-MUI),
[Mac OS X 10.4/10.5](https://github.com/retrolemmy/Diary-MacOSX) and
[Windows 98](https://github.com/retrolemmy/Diary-Win98) versions,
and any retro system that gets one later.

It's a single Python file with no dependencies beyond the standard library.
It speaks plain HTTP/1.0 (see [PROTOCOL.md](PROTOCOL.md)), stores entries as
UTF-8 text files, and gives every machine its entries in its own character
set: MacRoman for the classic Mac, ISO Latin-1 for the Amiga, UTF-8 for
Mac OS X, Windows-1252 for Windows, and NeXTSTEP's own character set. Search runs on the
server, so it's quick even on a 68000. Saving an entry that was changed on
another machine in the meantime is refused instead of overwriting it.

## In a browser

Any modern browser, on a computer, tablet or phone in your network, gets a
Diary of its own at the server's address, e.g. `http://192.168.1.10:8642/`:
a month calendar with the days that have an entry in bold, the list of
entries, search, and the open day to read and write. It saves when you
switch days, with the Save button or with Ctrl+S (⌘S), and like the retro
clients it asks before saving over an entry that was changed elsewhere in
the meantime. If the server has a key, the page asks for it once and the
browser remembers it. The page is built into `diary_server.py`; it needs no
internet connection.

## Running

```sh
python3 diary_server.py --dir ~/diary-entries
```

Options: `--port` (default 8642), `--bind` (address to listen on),
`--key SECRET` (or the `DIARY_KEY` environment variable) to require a key
from clients.

To bring in the entries you already have, copy a "Diary Entries" folder from
a Mac or an Amiga somewhere the server can read it, and run:

```sh
python3 diary_server.py --dir ~/diary-entries --import "path/to/Diary Entries"
```

Mac and Amiga files are told apart automatically.

### As a service on a Debian server (like RetroNAS)

```sh
sudo cp diary_server.py /usr/local/bin/
sudo cp diary-server.service /etc/systemd/system/
sudo systemctl enable --now diary-server
```

The service runs as the user `retro` and keeps the entries in
`/data/retronas/diary`; edit `diary-server.service` to change either.

To require a key, put it in `/etc/diary-server.env`, readable by root only
(service files can be read by every user), and restart the service:

```sh
echo 'DIARY_KEY=choose-something' | sudo tee /etc/diary-server.env > /dev/null
sudo chmod 600 /etc/diary-server.env
sudo systemctl restart diary-server
```

Then enter the key under **Diary Server...** in each Diary app; the web page
asks for it by itself. Choose a key that's easy to type on all your
machines: plain lowercase letters and digits travel best between old
keyboards and character sets.

## Security

Retro machines can't do HTTPS, so everything, including the key, travels
unencrypted. Run the server on your home network only and don't forward its
port to the internet.

## Testing

```sh
python3 -m unittest -v test_server
```

## License

No rights reserved: Diary Server is dedicated to the public domain under
[CC0 1.0](LICENSE). Use it for anything, no attribution needed.
