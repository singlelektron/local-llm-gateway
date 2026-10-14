# Third-party notices

The gateway implementation is original; no upstream gateway or inference-engine
implementation is copied or vendored here. Dependencies are installed separately
from their upstream distributions, with versions and artifact hashes recorded in
[`uv.lock`](uv.lock). Their licenses remain applicable to those distributions.
This file does not grant a license to this project's own source; the owner has
not selected a project license.

## Runtime packages

The following inventory was checked against the lockfile, installed distribution
metadata, and the distributions' actual license files on 2026-10-14. License paths
are relative to each package's `.dist-info/licenses/` directory. The conditional
Python 3.12 dependency `typing-extensions` was checked against its release
metadata and [upstream license](https://github.com/python/typing_extensions/blob/4.16.0/LICENSE).

| Package and upstream release | License | License/notice files and attribution |
| --- | --- | --- |
| [aiohttp 3.14.5](https://pypi.org/project/aiohttp/3.14.5/) | Apache-2.0 AND MIT | `LICENSE.txt`: aio-libs contributors. `vendor/llhttp/LICENSE`: bundled llhttp parser, copyright 2018 Fedor Indutny, MIT. Both apply. |
| [aiohappyeyeballs 2.7.1](https://pypi.org/project/aiohappyeyeballs/2.7.1/) | PSF-2.0 | `LICENSE`, including its retained Python historical license notices. |
| [aiosignal 1.4.0](https://pypi.org/project/aiosignal/1.4.0/) | Apache-2.0 | `LICENSE`: copyright 2013–2019 Nikolay Kim and Andrew Svetlov. |
| [attrs 26.1.0](https://pypi.org/project/attrs/26.1.0/) | MIT | `LICENSE`: copyright 2015 Hynek Schlawack and the attrs contributors. |
| [frozenlist 1.8.0](https://pypi.org/project/frozenlist/1.8.0/) | Apache-2.0 | `LICENSE`: copyright 2013–2019 Nikolay Kim and Andrew Svetlov. |
| [idna 3.20](https://pypi.org/project/idna/3.20/) | BSD-3-Clause | `LICENSE.md`: copyright 2013–2026 Kim Davies and contributors. |
| [multidict 7.1.0](https://pypi.org/project/multidict/7.1.0/) | Apache-2.0 | `LICENSE`: aio-libs contributors. |
| [propcache 0.5.4](https://pypi.org/project/propcache/0.5.4/) | Apache-2.0 | `LICENSE` and `NOTICE`: copyright 2016–2021 Andrew Svetlov and aio-libs team. |
| [typing-extensions 4.16.0](https://pypi.org/project/typing-extensions/4.16.0/) | PSF-2.0 | `LICENSE`, including its retained Python historical license notices; installed for Python below 3.13. |
| [yarl 1.25.1](https://pypi.org/project/yarl/1.25.1/) | Apache-2.0 | `LICENSE` and `NOTICE`: copyright 2016–2021 Andrew Svetlov and aio-libs team. |

These licenses permit the current use of the unmodified libraries by the gateway.
Keep the full upstream license and notice files when distributing the libraries
or a bundled environment; this inventory does not replace those files. The
current gateway wheel does not bundle these dependencies.

## Build packages

Hatchling and its build dependencies are in the locked `dev` dependency group.
The verified build path is `uv sync --frozen` followed by
`uv build --no-build-isolation`, using those installed versions. A separate
isolated build can resolve different transitive build versions.

| Package and upstream release | License | Checked files and attribution |
| --- | --- | --- |
| [hatchling 1.27.0](https://pypi.org/project/hatchling/1.27.0/) | MIT | `licenses/LICENSE.txt`: copyright 2021–present Ofek Lev. |
| [packaging 26.3](https://pypi.org/project/packaging/26.3/) | Apache-2.0 OR BSD-2-Clause; embedded parser notice: MIT | `licenses/LICENSE`, `LICENSE.APACHE`, `LICENSE.BSD`: Donald Stufft and individual contributors. `packaging/licenses/__init__.py` also retains an MIT notice for code adapted from Hatchling, copyright 2017–present Ofek Lev. |
| [pathspec 1.1.1](https://pypi.org/project/pathspec/1.1.1/) | MPL-2.0 | `licenses/LICENSE`; used unchanged as a build tool. Its source distribution is linked and hashed in `uv.lock`. |
| [pluggy 1.6.0](https://pypi.org/project/pluggy/1.6.0/) | MIT | `licenses/LICENSE`: copyright 2015 holger krekel. |
| [trove-classifiers 2026.9.21.13](https://pypi.org/project/trove-classifiers/2026.9.21.13/) | Apache-2.0 | `licenses/LICENSE`. |

Paths beginning `licenses/` above are relative to the package's `.dist-info/`
directory. The gateway neither modifies nor includes these tools in its runtime
wheel. MPL-2.0 applies to pathspec's covered files; retain its license and source
availability when redistributing that tool. Hatchling's conditional `tomli`
dependency is for Python below 3.11 and is not used by this project's supported
Python versions.

## Development and CI tools

The workflow invokes these external tools without copying their implementations
into the gateway. The action licenses were read at the exact pinned revisions.
Their own distributions retain notices for their dependencies.

| Tool | Pinned revision/version | License source |
| --- | --- | --- |
| actions/checkout | `d23441a48e516b6c34aea4fa41551a30e30af803` (v6) | [MIT; GitHub, Inc. and contributors](https://github.com/actions/checkout/blob/d23441a48e516b6c34aea4fa41551a30e30af803/LICENSE) |
| actions/setup-python | `ece7cb06caefa5fff74198d8649806c4678c61a1` (v6) | [MIT; GitHub, Inc. and contributors](https://github.com/actions/setup-python/blob/ece7cb06caefa5fff74198d8649806c4678c61a1/LICENSE) |
| astral-sh/setup-uv | `37802adc94f370d6bfd71619e3f0bf239e1f3b78` (v7) | [MIT; Kevin Stillhammer](https://github.com/astral-sh/setup-uv/blob/37802adc94f370d6bfd71619e3f0bf239e1f3b78/LICENSE) |
| uv | `0.13.0` in CI | [MIT](https://github.com/astral-sh/uv/blob/0.13.0/LICENSE-MIT) OR [Apache-2.0](https://github.com/astral-sh/uv/blob/0.13.0/LICENSE-APACHE); Astral Software Inc. |

Ollama, Python, SQLite, and model weights are supplied by the user's environment;
this repository does not redistribute them or download model weights. Their own
distributions and selected model licenses remain the source of their terms.
Recheck this inventory when updating dependencies, vendoring code, or preparing a
distribution that bundles tools, libraries, or models.
