# Contributing to AirLinx

Bug reports and questions go to [GitHub Issues](https://github.com/TymonPawelczyk/AirLinx/issues). Issues are public, so leave out personal data, passwords and full logs with tokenised URLs. Security problems go through private reporting instead (see [SECURITY.md](SECURITY.md)).

For a bug, include your distribution and version, the session type (`echo $XDG_SESSION_TYPE`), the GPU, the TV model, the protocol (Cast or DLNA) and the steps that lead to the problem. Output from `airlinx --debug` helps.

## Branches (Git Flow)

| Branch | Starts from | Merges into | Purpose |
|---|---|---|---|
| `main` | | | Released code only. Every merge is a release and gets a tag. |
| `develop` | `main` | | Integration branch. CI must be green here. |
| `feature/<short-name>` | `develop` | `develop` | New features and non-urgent fixes, e.g. `feature/vaapi-encoder`. |
| `release/X.Y.Z` | `develop` | `main` and `develop` | Release preparation: version, changelog, docs. No new features. |
| `hotfix/X.Y.Z` | `main` | `main` and `develop` | Urgent fixes to a released version. |

- Open pull requests against `develop` (features) or `main` (release and hotfix branches only). Contributors do not merge; the maintainer merges.
- Merge with `--no-ff` so each feature, release and hotfix stays visible as one merge commit.
- After a release or hotfix branch is merged into `main`, the merge commit on `main` is tagged `vX.Y.Z` (annotated tag) and the branch is merged back into `develop`.
- Versions follow [Semantic Versioning](https://semver.org/). The version lives in `airlinx/__init__.py` (`__version__`). Every release adds a section to [CHANGELOG.md](CHANGELOG.md) and release notes in `docs/release-notes/vX.Y.Z.md`.
- Pushing to `main` deploys `site/` to GitHub Pages (`.github/workflows/pages.yml`).

## Commit messages

Use [Conventional Commits](https://www.conventionalcommits.org/) in English:

```
fix(controller): ask before downgrading a pinned TV from Cast to DLNA
feat: choose the audio source sent to the TV
docs(legal): reflect GPL-3.0-or-later in legal documents
```

Common types: `feat`, `fix`, `docs`, `test`, `ci`, `chore`, `refactor`. The scope is optional and usually names the module (`controller`, `mirror`, `dlna`, `site`). Say why in the body when the subject does not make it obvious.

## Development setup and tests

```bash
uv venv --python /usr/bin/python3 --system-site-packages .venv
uv pip install --python .venv/bin/python -r requirements-dev.txt
.venv/bin/python -m pytest -q        # offline suite, must pass before every pull request
```

The venv needs `--system-site-packages` because PyGObject (GTK 3, WebKit2 4.1) comes from the system. [docs/TESTING.md](docs/TESTING.md) describes every test layer, the `e2e`, `tv` and `slow` markers and what CI runs. Change the module contract in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) before changing the code that implements it.

## Testing on a real TV

Live tests (`pytest -m tv tests/live` with `AIRLINX_TV_IP`) and the smoke scripts in `scripts/` talk to a real TV. Rules:

- Keep runs short.
- **Never use `force`** (`--force`, `force: true`). The tests and scripts refuse to start when another app is running on the TV, because someone may be watching it.
- Afterwards, check that no `ffmpeg` process was left behind (`pgrep ffmpeg`).

## Language

- Everything the user sees in the app and on the landing page is **Polish with full diacritics** (ą, ć, ę, ł, ń, ó, ś, ź, ż).
- Code, identifiers, comments, logs, commit messages and English documentation (README, CHANGELOG, this file) are in **English**.

## License

By contributing, you agree that your contribution is licensed under the project's license, GPL-3.0-or-later (see [LICENSE](LICENSE)).
