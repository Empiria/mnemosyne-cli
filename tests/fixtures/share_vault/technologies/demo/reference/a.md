---
tags:
  - share:test
---

# Note A

Edge case 1 (body link): links to b.
Body link: [[technologies/demo/reference/b]]

Edge case 5 (circular): links to e, and e links back.
Circular link: [[technologies/demo/reference/e]]

Edge case 6 (in-set-to-exclude): links to the ADR.
Exclude link: [[technologies/demo/decision/adr-1]]

Edge case 8 (embed): embeds f.
Embed: ![[technologies/demo/reference/f]]

Edge case 7 (in-set-to-tag-included): links to shared-note, which is outside
include.paths but carries the share:test tag — enters seed via tag.
Tag-included link: [[technologies/other/shared-note]]

Closure breach (leaky): links to a note outside include and exclude.
Breach link: [[technologies/secret/leaky]]
