# Diary Server protocol 1.0

Plain HTTP/1.0 over TCP, one request per connection, made for 68k machines:
every response has a `Content-Length` and `Connection: close`, all bodies are
plain text, and nothing needs chunked encoding, cookies or TLS.

## Character sets and line ends

The server stores entries as UTF-8 with LF line ends. A client names its own
character set with the `charset` query parameter on any request; texts it
sends and receives (and its search words) are then in that character set:

| `charset=` | for | default line end |
|---|---|---|
| `utf-8` (default) | modern systems | LF |
| `latin1` | AmigaOS | LF |
| `macroman` | classic Mac OS | CR |
| `nextstep` | NeXTSTEP | LF |
| `windows-1252` | Windows 95/98 | CR LF |

`eol=lf`, `eol=cr` or `eol=crlf` overrides the line end. Characters the
client's character set lacks are sent as plain look-alikes (`"` for curly
quotes, `-` for dashes, `...`) or `?`.

## Requests

### `GET /entries`

All days with an entry, newest first, one per line, with the entry's version:

```
2026-10-29 3fa1c2d4
2026-10-27 0b9e771a
```

### `GET /entries/YYYY-MM-DD`

The entry's text, without a final line end. The response header
`X-Diary-Version` holds its version. `404` if there's no entry for that day.

### `PUT /entries/YYYY-MM-DD`

Saves the request body as the entry; an empty body (or one with only line
ends) deletes it. Send `X-Diary-Base` with the version the text was based
on: the version from the `GET`, or `none` for a day that had no entry.
If the entry has changed since, the server refuses with `409` and sends the
current version in `X-Diary-Version`, so nothing is overwritten unseen.
Without `X-Diary-Base`, the entry is always replaced.

On success: `200`, and the new version (or `none` if deleted) in
`X-Diary-Version` and as the body.

### `DELETE /entries/YYYY-MM-DD`

Deletes the entry; `X-Diary-Base` works as for `PUT`.

### `GET /search?q=WORDS`

Days whose entry contains `WORDS` (percent-encoded, in the client's
character set), ignoring case; newest first, one per line.

### `GET /`

A short status text. A browser (a request whose `Accept` header includes
`text/html`) gets the web page instead: a Diary that runs in the browser
and uses the requests below. The page needs no key; it asks for one.

## Keys

If the server is started with `--key`, every request must carry it, in the
header `X-Diary-Key` or as the query parameter `key`. Otherwise it gets `403`.
This keeps other people on the network out; it isn't encryption.

## Errors

`400` bad parameter, `403` wrong key, `404` unknown day or path,
`409` conflict, `411` missing `Content-Length`, `413` entry over 64 KB.
The body is a one-line explanation.
