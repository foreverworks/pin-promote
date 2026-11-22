# Config reference

pin-promote reads one JSON file from the repository it promotes into. By default that is
`pin-promote.json` at the repository's root; `--config FILE` names another, relative to the
repository (`-C DIR`, which defaults to the current directory). `-C` goes before the command:
`pin-promote -C path/to/repo check --config ci/pins.json`.

The whole file is checked before anything else happens, and every problem is reported at once, one
per line, each starting with the file's path and the key it is about. `pin-promote check` runs only
this check and prints the channels, so it is the thing to put in CI. Any key this version does not
know is an error, at every level: a misspelled `chekcs` must not silently mean "no checks".

A complete example is [examples/pin-promote.json](../examples/pin-promote.json).

```json
{
  "schema": 1,
  "branch": "main",
  "remote": "origin",
  "attempts": 3,
  "shared_sources": ["lock.json"],
  "journal": ["CHANGELOG.md"],
  "channels": {
    "app": {
      "pin": "deploy/app.json",
      "fields": { "digest": "sha256:[0-9a-f]{64}" },
      "sources": ["app/", "Dockerfile"],
      "checks": ["test", "smoke"],
      "subject": "app: pin the verified image",
      "note": "Tagged stable in the registry.",
      "registry": { "image": "registry.example.com/team/app", "tag": "stable", "field": "digest" }
    }
  }
}
```

## Top level

| Key | Type | Default | Rule |
|---|---|---|---|
| `schema` | integer | none, required | Must be `1`. A config for another schema is refused rather than guessed at. |
| `branch` | string | `"main"` | The branch the pins live on and are pushed to. Letters, digits, `.`, `_`, `/` and `-`; no `..`. |
| `remote` | string | `"origin"` | The git remote to fetch from and push to. Letters, digits, `.`, `_` and `-`. |
| `attempts` | integer | `3` | How many times a push the remote refused is tried again from the new tip. A whole number from 1 to 20 (a boolean is not a number). |
| `shared_sources` | list of strings | `[]` | Git pathspecs that every channel's build must not be older than (a lock file, a shared base). Each is a repository-relative pathspec, see [Pathspecs](#pathspecs). |
| `journal` | list of strings | `[]` | Files that get one appended entry per promotion (a changelog), created if absent. Each is a repository-relative file path. A channel's pin file cannot be a journal file. |
| `channels` | object | none, required | Channel name to channel object. At least one. No two channels may write the same `pin` file. |

## Channels

A channel is one pin file, and the rules for moving it. Its name is the key in `channels`: lower
case letters, digits, `.`, `_` and `-`, starting with a letter or a digit. The command line names a
pin as `CHANNEL.FIELD=VALUE` and splits at the first `.`, so give a channel a name without a `.`
(`vm-image`, not `vm.image`).

| Key | Type | Default | Rule |
|---|---|---|---|
| `pin` | string | none, required | Repository-relative path of the pin file, ending in `.json`. No `..`, no leading `/`, no backslash. |
| `fields` | object | none, required | Field name to the regular expression its value must match completely. At least one field. See [Fields](#fields). |
| `sources` | list of strings | `[]` | Git pathspecs this channel's build must not be older than. See [Pathspecs](#pathspecs). A list of exclusions alone is refused: it selects nothing. |
| `checks` | list of strings | none, required | The checks that must all have passed before this channel's pin moves. At least one, each a non-empty string, none listed twice. A pin never moves unchecked. |
| `subject` | string | `"<name>: pin the verified build"` | The commit's subject line when this channel moves. One non-empty line. When one promotion moves several channels, their subjects are joined with `; `. |
| `note` | string | `""` | Text appended to this channel's line in each journal file. At most one line; may be empty. |
| `registry` | object | none | Moves a registry tag to the pinned digest once the pin is on the branch. See [Registry](#registry). |

### Fields

`fields` lists what the pin file holds. Each key is a field name: ASCII letters, digits and `_`
(and `.` or `-` after the first character), starting with a letter or a digit. Each value is a
regular expression in Python's `re` syntax. A value passed with `--pin` must match it from its
first character to its last (`re.fullmatch`), so `sha256:[0-9a-f]{64}` accepts exactly a lower case
digest and nothing around it. The expression must compile, and must not match the empty string.

Every field must be given on every promotion, and no other: a pin is one consistent set of values,
never half of one. The pin file is written as a single JSON object on one line, with the fields in
the order `fields` lists them, so the file never changes just because the order of the command line
did.

### Pathspecs

`sources` and `shared_sources` take git pathspecs relative to the repository root: a file
(`Dockerfile`), a directory (`app/`, with or without the slash), or one of those prefixed with
`:!` (or `:^`) to exclude it (`:!app/README.md`). Other pathspec magic, such as `:(glob)` or
`:(top)`, is refused, because it changes what a path means. A path cannot be absolute, contain
`..` or a backslash, or be empty.

A build is stale for a channel when any file selected by that channel's `sources`, the
`shared_sources` or the channel's own `pin` file differs between the build's commit and the
branch's tip. The pin file is always included, so a build older than the pin's last move is
refused even if you list no sources.

### Registry

A channel with a `registry` block moves a tag in a container registry to the digest the pin holds,
after the pin is on the branch. Credentials come from the environment, not from the config (see the
README).

| Key | Type | Default | Rule |
|---|---|---|---|
| `image` | string | none, required | Registry host and repository: lower case, `registry.example.com/team/app`, or with a port, `localhost:5000/app`. The first part is always the registry host. No tag and no digest. |
| `tag` | string | none, required | The tag to move: letters, digits, `_`, `.` and `-`, starting with a letter, digit or `_`, at most 128 characters. |
| `field` | string | none, required | The channel field that holds the digest. It must be one of the channel's `fields`, and its pattern must accept only `sha256:` followed by 64 lower case hex digits (a pattern that would let `latest`, a bare `sha256:` or an upper case digest through is refused). |

## What the config does not hold

- Which build is being promoted, the new pin values and which checks passed are not config: they
  are the arguments of `pin-promote promote`.
- Registry credentials are the environment variables `PIN_PROMOTE_REGISTRY_USERNAME` and
  `PIN_PROMOTE_REGISTRY_PASSWORD`.
