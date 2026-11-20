"""Signals of the file features (contract W5, briefs/wave2-files.md).

changed(sender, source, paths): sent after an operation changed files on a storage source, once it is
done: mkdir, rename, move, copy, delete (the files app), uploads and text saves (the uploads app).
`source` is the StorageSource, `paths` the storage paths that changed (both ends of a move or rename).
Receivers must be quick and must not fail the operation: office uses it to forget the OnlyOffice
document keys of those paths.
"""

from django.dispatch import Signal

changed = Signal()
