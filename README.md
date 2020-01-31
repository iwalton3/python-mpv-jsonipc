# Python MPV JSONIPC

This implements an interface similar to `python-mpv`, but it used the JSON IPC protocol instead of the C API. This means
you can control external instances of MPV including players like SMPlayer, and it can use MPV players that are prebuilt
instead of needing `libmpv1`. It may also be more resistant to crashes such as Segmentation Faults, but since it isn't
directly communicating with MPV via the C API the performance will be worse.


