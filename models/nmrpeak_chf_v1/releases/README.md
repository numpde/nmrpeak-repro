# CHF checkpoint releases

This directory contains reviewed committed declarations consumed by
`make checkpoint/import`. `make release/write` emits candidate bytes for a new
declaration; it never installs, replaces, or loads a checkpoint. After review,
`make release/install` revalidates and installs an absent declaration without
replacing an existing release.
