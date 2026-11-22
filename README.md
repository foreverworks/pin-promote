# pin-promote

**Move a repository's pins only after their checks passed, and never from a build older than the
files it was built from.** A pin is a small JSON file in your repository that says which build is
the current one: an image digest, a VM image fingerprint, a release checksum. pin-promote is the
one step that changes it, so a pin can only ever point at something that was tested, and can never
go backwards.

For teams whose CI builds an artifact, tests it, and then records "this one is good" in git so a
deploy can read it.

Python standard library only. Typed. MIT.

## The problem

Most pipelines have a step that writes the new digest into a file and pushes it. It usually works.
Then:

- **A pin moves to something nobody checked.** The build job pushes the pin as soon as the image
  exists, and the smoke test fails a minute later. Production now points at a broken image, and the
  pin's history says it was fine.
- **An older build overwrites a newer one.** Two merges land ten minutes apart. The first build is
  slow and finishes last, so its pin write lands after the second one's and quietly undoes it. The
  same happens to a lock file regenerated from an old checkout.
- **A retry makes a second commit, or a mess.** A push is refused because the branch moved, the job
  is re-run, and now there are two "promote" commits, or none, and nobody is sure which.

pin-promote makes the promotion a single, checked step. It writes nothing unless every required
check passed, it refuses a build that is older than the sources it was built from, it retries a
refused push from the new tip of the branch, and it never force-pushes.

## What you get

- **A config** (`pin-promote.json`) listing your channels: for each, the pin file, the fields it
  holds and the pattern each value must match, the paths a build must not be older than, and the
  checks that must have passed.
- **`pin-promote promote`**, the one command that moves pins, with the steps listed below.
- **`check`, `show` and `tag`**: validate the config, print what the pins hold on the branch, and
  retry a registry tag.
- **An optional registry tag**: after a pin moves, a tag such as `stable` is pointed at the pinned
  digest, so what the registry calls stable and what git pins cannot disagree.
- **A single-file release**: `pin-promote-X.Y.Z.pyz` and its checksum, one file for any Python 3.13
  or newer.

## Five-minute quick start

**1. Install.** One file, any Python 3.13 or newer:

```sh
curl -fsSLO https://github.com/foreverworks/pin-promote/releases/download/v0.1.0/pin-promote-0.1.0.pyz
curl -fsSLO https://github.com/foreverworks/pin-promote/releases/download/v0.1.0/pin-promote-0.1.0.pyz.sha256
sha256sum -c pin-promote-0.1.0.pyz.sha256
python3 pin-promote-0.1.0.pyz --version
```

or `pip install git+https://github.com/foreverworks/pin-promote@v0.1.0`, which installs the
`pin-promote` command and the `pin_promote` library. The examples below write `pin-promote`; with
the file, write `python3 pin-promote-0.1.0.pyz`.

**2. Write a config** at the root of the repository you want to protect, as `pin-promote.json`
([examples/pin-promote.json](examples/pin-promote.json) has two channels). This one has one
channel, `app`, whose pin is `deploy/app.json`:

```json
{
  "schema": 1,
  "journal": ["CHANGELOG.md"],
  "channels": {
    "app": {
      "pin": "deploy/app.json",
      "fields": { "digest": "sha256:[0-9a-f]{64}" },
      "sources": ["app/", "Dockerfile"],
      "checks": ["test", "smoke"]
    }
  }
}
```

`fields` says what the pin holds and the pattern a value must match in full. `sources` are the
paths the image is built from. `checks` are the names of the checks that must have passed. Every
key is explained in [docs/config.md](docs/config.md).

**3. Check it.**

```sh
$ pin-promote check
app: deploy/app.json after test, smoke
$ pin-promote show
{
  "app": null
}
```

`check` exits 1 and lists every problem if the config is wrong. `show` prints each pin as the
remote branch holds it (`null`: no pin file yet).

**4. Try a promotion without committing anything.** Push the commit you built from to `main`
first: pin-promote only promotes builds that are on the branch.

```sh
pin-promote promote --dry-run \
  --build "$(git rev-parse HEAD)" \
  --pin app.digest=sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef \
  --passed test --passed smoke
would promote app in 2380cfdfdd52ac34c30f797a250094e4f0ec25bc
```

Leave out `--passed smoke` and it refuses, which is the point:

```text
refused: checks not passed: smoke (not run)
```

**5. Do it for real from CI.** Drop `--dry-run` and run the same command as the last job of your
pipeline, see [GitHub Actions](#github-actions) below. pin-promote commits the pin (and a journal
entry), pushes it, and your deploy reads `deploy/app.json`.

## What a promotion does

In this order. Any refusal stops everything before a commit is made.

1. **Every check the named channels require has passed.** Evidence is `--passed NAME` for a check
   your job ran itself, and `--needs-json` / `--needs-file` for a CI system's job results. Only a
   result of `success` counts: `failure`, `cancelled`, `skipped` and a job that never ran do not.
2. **Every new value matches its field's pattern**, and every field of the channel is given. A pin
   is one set of values, never part of one.
3. **The branch is fetched, and the build's commit must be on it.** A build of a feature branch
   never promotes.
4. **A channel whose pin already holds these values is unchanged.** A retry commits nothing.
5. **For every channel that changes, the build is not stale.** No file in that channel's `sources`,
   the config's `shared_sources` or the channel's own pin file may differ between the build's
   commit and the branch's tip. If one does, someone changed what this build was made from, or
   promoted a newer build, since it started: the whole promotion is refused.
6. **The pins are written in a temporary worktree** of the branch's tip, with one journal entry per
   `journal` file, committed and pushed. Your own checkout is never touched. The push is never
   forced: if the remote refuses it because the branch moved, pin-promote fetches again and repeats
   steps 3 to 6 from the new tip, up to `attempts` times (3 by default).
7. **The registry tag follows the pin**, for a channel with a `registry` block: see
   [Registry tags](#registry-tags).

The commit looks like this, and the journal gets the same facts as a short entry:

```text
app: pin the verified image

app: digest `sha256:0123...cdef`

Build: 2380cfdfdd52ac34c30f797a250094e4f0ec25bc
Run: https://ci.example.com/run/1
Checks passed: test, smoke
```

Exit status: `0` promoted or nothing to do, `1` refused or failed (the message says which, on
standard error), `2` a usage error. `--json` prints the result as one JSON object with `commit`,
`promoted`, `unchanged`, `attempts`, `dry_run` and `tags_moved`.

## GitHub Actions

The promotion is the last job. It `needs` every job whose result matters, and hands their results
over as they are: `toJSON(needs)` is `{"<job>": {"result": "success", ...}}`, which is the shape
`--needs-json` reads. The config's `checks` names are job ids, so `test` and `smoke` in the config
are the jobs called `test` and `smoke` below.

```yaml
name: build
on:
  push:
    branches: [main]

permissions:
  contents: read

jobs:
  build:
    runs-on: ubuntu-latest
    outputs:
      digest: ${{ steps.push.outputs.digest }}
    steps:
      # ... build and push the image, and write its digest to $GITHUB_OUTPUT as `digest`

  test:
    needs: [build]
    runs-on: ubuntu-latest
    steps:
      # ...

  smoke:
    needs: [build]
    runs-on: ubuntu-latest
    steps:
      # ...

  promote:
    needs: [build, test, smoke]
    if: ${{ !cancelled() }} # run even when a check failed: pin-promote says which, and refuses
    runs-on: ubuntu-latest
    concurrency:
      group: promote
      cancel-in-progress: false # let a running promotion finish, then the next one
    permissions:
      contents: write # the push of the pin
      actions: write # the dispatch of the deploy
      packages: write # only for a registry tag on ghcr.io
    steps:
      - uses: actions/checkout@v7 # pin it to a commit SHA, as for any action
        with:
          fetch-depth: 0 # the whole history: "is the build on main" and "is it stale" read it
          # persist-credentials stays on: pin-promote pushes with this checkout's token

      - name: Install pin-promote, checked against its checksum
        run: |
          base=https://github.com/foreverworks/pin-promote/releases/download/v0.1.0
          curl -fsSLO "$base/pin-promote-0.1.0.pyz"
          curl -fsSLO "$base/pin-promote-0.1.0.pyz.sha256"
          sha256sum -c pin-promote-0.1.0.pyz.sha256

      - name: Promote
        env:
          NEEDS: ${{ toJSON(needs) }}
          DIGEST: ${{ needs.build.outputs.digest }}
          PIN_PROMOTE_REGISTRY_USERNAME: ${{ github.actor }}
          PIN_PROMOTE_REGISTRY_PASSWORD: ${{ github.token }}
        run: |
          python3 pin-promote-0.1.0.pyz promote \
            --build "$GITHUB_SHA" \
            --pin "app.digest=$DIGEST" \
            --needs-json "$NEEDS" \
            --run-url "$GITHUB_SERVER_URL/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID" \
            --author "github-actions[bot] <41898282+github-actions[bot]@users.noreply.github.com>"

      - name: Dispatch the deploy
        env:
          GH_TOKEN: ${{ github.token }}
        run: gh workflow run deploy.yml --ref main
```

Things people trip over:

- **`--author` is needed on a runner**, which has no git identity: without it the commit fails with
  "Please tell me who you are". It sets the author and the committer of the promotion commit.
- **Values go through `env`, never into the script text.** `${{ needs.build.outputs.digest }}` pasted
  into a `run:` line is code injection if the output is ever not what you expect. pin-promote also
  rejects a value that does not match the field's pattern, but the shell comes first.
- **Branch protection that requires pull requests blocks the push.** The promotion is a direct push
  to the branch. Allow `github-actions[bot]` to bypass the rule for it, or promote on a branch of
  its own (`"branch": "pins"`) that your deploy reads.
- **A check that failed shows up as a refusal, not a skipped job.** That is why the job runs on
  `!cancelled()`: `refused: checks not passed: smoke (failure)` in the log is easier to act on than
  a grey "skipped" box.
- **A refused promotion for a stale build is normal, not an outage.** It means a newer build has
  been or will be promoted. Let the job fail, or add `continue-on-error: true` if you would rather
  not see it red.

### Retry, and dispatch after

A push made with the workflow's `GITHUB_TOKEN` does not start other workflows. GitHub does this on
purpose, to stop loops. So a deploy that is triggered by "a push to `deploy/app.json`" will never
run after pin-promote. Do not rely on the push; **dispatch the deploy explicitly after promote**,
as in the last step above (`gh workflow run`, with `actions: write`, and `workflow_dispatch:` in the
deploy workflow).

This also makes a re-run safe. If the promote job is re-run (a flaky push, a failed tag move), the
pin already holds these values, so promote commits nothing and reports `unchanged`. The dispatch
step then runs again, which is what you want: the deploy has to happen even when this run did not
make the commit. Keep the deploy idempotent (deploying the pinned digest twice changes nothing) and
put the dispatch after promote, unconditionally.

If the remote refuses the push because the branch moved while the job ran, pin-promote retries it
from the branch's new tip by itself, and checks staleness again on the way, so a retry cannot
slip a stale build through.

## Registry tags

Git pins the digest, but people and tools also want a name: `registry.example.com/team/app:stable`.
A channel with a `registry` block points that tag at the digest the pin holds, after the pin is on
the branch:

```json
"registry": { "image": "registry.example.com/team/app", "tag": "stable", "field": "digest" }
```

The tag follows the pin, never the other way around. pin-promote reads the manifest by digest,
checks that its bytes hash to that digest, and puts the same bytes under the tag. If the tag is
already there, it does nothing, so running it again is harmless.

Credentials are the environment variables `PIN_PROMOTE_REGISTRY_USERNAME` and
`PIN_PROMOTE_REGISTRY_PASSWORD`; for GitHub's registry, the actor and a token with `packages: write`
as in the example. They are sent only to the registry, or to the token service its challenge names,
over https. A registry that redirects is an error: a redirect would carry the credentials somewhere
else. For a local test registry on plain http, pass `--plain-http`.

If the tag move fails (the registry was down), the pin is already on the branch, and promote exits
1. Run the job again: the pin is unchanged, and the tag is moved. Or move only the tag, which needs
no new build:

```sh
pin-promote tag --channel app
```

## Supported Pythons

3.13, 3.14 and 3.15. Every push is tested on 3.15, the newest stable release, with the lint and the
type check (`mypy --strict`) run against 3.13, the oldest supported one. Once a week the whole suite
runs on all three, and on the next pre-release (3.16) as an early warning that may fail without
blocking anything. A release is published only after the full matrix passed.

## Releases

A tag `vX.Y.Z` on a commit of `main` publishes `pin-promote-X.Y.Z.pyz` and
`pin-promote-X.Y.Z.pyz.sha256` as a GitHub release, once the tests and checks pass, and only if the
tag equals the version in the source. The file is reproducible: `scripts/build-pyz` on that tag
gives the same bytes, so you can rebuild it and compare checksums instead of trusting the upload.

```sh
curl -fsSLO https://github.com/foreverworks/pin-promote/releases/download/vX.Y.Z/pin-promote-X.Y.Z.pyz
curl -fsSLO https://github.com/foreverworks/pin-promote/releases/download/vX.Y.Z/pin-promote-X.Y.Z.pyz.sha256
sha256sum -c pin-promote-X.Y.Z.pyz.sha256
```

## Using it as a library

```python
from pathlib import Path
from pin_promote import load, promote

repo = Path(".")
config = load(repo / "pin-promote.json")
result = promote(
    repo,
    config,
    {
        "build": "2380cfdfdd52ac34c30f797a250094e4f0ec25bc",
        "pins": {"app": {"digest": "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"}},
        "results": {"test": "success", "smoke": "success"},
        "run_url": "",
    },
)
```

`PromotionError` means refused, with nothing pushed; `ConfigError` means the config is wrong.

## License

MIT. See [LICENSE](LICENSE).
