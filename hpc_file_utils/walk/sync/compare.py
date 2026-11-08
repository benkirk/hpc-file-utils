"""walksync's decision for one entry: copy it, or leave it (pure: no MPI, no I/O).

The quick check rsync itself makes, on stats already fetched:

  regular file   copy if missing, a different type, a different size, or a different
                 whole-second mtime
  symlink        copy if missing, a different type, or a different target (its times are
                 not compared: not every system can set a symlink's)
  directory      copy (create) if missing or a different type; its mode and times are
                 left to the final serial rsync
  other          (fifo, socket, device) copy if missing or a different type, or for a
                 device a different st_rdev

Ownership, permissions, ACLs and xattrs are not compared, as rsync's quick check does not
compare them either: the final serial rsync -aHAX --delete is the authoritative pass.
"""

import stat

NEW, TYPE, SIZE, MTIME, LINK, RDEV = 'new', 'type', 'size', 'mtime', 'link', 'rdev'


def needs_copy(src_st, dest_st, src_link=None, dest_link=None):
    """Why the entry with lstat 'src_st' must be copied over 'dest_st', or None if not.

    dest_st is None when the destination has no such name. src_link/dest_link are the
    readlink() targets, needed only when both are symlinks.
    """
    if dest_st is None:
        return NEW
    smode, dmode = src_st.st_mode, dest_st.st_mode
    if stat.S_IFMT(smode) != stat.S_IFMT(dmode):
        return TYPE
    if stat.S_ISREG(smode):
        if src_st.st_size != dest_st.st_size:
            return SIZE
        if int(src_st.st_mtime) != int(dest_st.st_mtime):
            return MTIME
        return None
    if stat.S_ISLNK(smode):
        return LINK if src_link != dest_link else None
    if stat.S_ISBLK(smode) or stat.S_ISCHR(smode):
        return RDEV if src_st.st_rdev != dest_st.st_rdev else None
    return None   # a directory or a special file of the same type


def extraneous(src_names, dest_names, exclude=None):
    """The destination's names the source does not have (never deleted; counted, and
    listed with --dry-run), in sorted order. Names matching --exclude are not counted:
    the walk never looked for them in the source."""
    extra = set(dest_names).difference(src_names)
    if exclude is not None:
        extra = {n for n in extra if not exclude(n)}
    return sorted(extra)


def show(path):
    """A path printable on one line, whatever bytes it holds: a newline or tab as \\n or
    \\t, a byte that is not UTF-8 as \\xNN (for reading, not for parsing back)."""
    text = path.encode('utf-8', 'surrogateescape').decode('utf-8', 'backslashreplace')
    return text.replace('\n', '\\n').replace('\t', '\\t')
