#!/usr/bin/env python3


import sys, os, stat, time

GPFS_BLOCKSIZE=512

# takes an integer number of bytes and returns a human-readable string
def human_readable_bytes(num_bytes):
    units = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB', 'EiB']
    size = float(num_bytes)
    for unit in units:
        if size < 1024.0:
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{size:.2f} {units[-1]}"



# function to read a NULL-separated file list from stdin, splitting into individual files
def read_null_terminated_filenames(stream, chunk_size=4096):
    buffer = b''
    while True:
        chunk = stream.read(chunk_size)
        if not chunk:
            break
        buffer += chunk
        while b'\0' in buffer:
            filename, buffer = buffer.split(b'\0', 1)
            if filename:
                yield filename.decode('utf-8', errors='replace')
    # Handle any remaining data (in case there's no trailing null)
    if buffer:
        yield buffer.decode('utf-8', errors='replace')



def process_file(filepath):

    #print(f"Processing: {filepath}")

    stat_info = os.stat(filepath)

    # decode file type - should be a regular file only; (output from find -type f)
    fmode = stat_info.st_mode

    assert stat.S_ISREG(fmode)

    # about stat_info.st_size: we can't always trust it.
    # for sparse files, or files migrated to take via HSM,
    # the size is not what is occupied on disk.
    # a better metric is (number of blocks * fs_block_size);

    # however the block size is variable across filesystems / GPFS file sets
    # for regular, uncompressed, unmigrated files; size <= (n_blocks*fs_block_size)
    file_true_size = stat_info.st_size
    file_nblocks = stat_info.st_blocks
    file_ondisk_size = min(file_true_size, file_nblocks*GPFS_BLOCKSIZE)

    return file_ondisk_size, file_true_size



def main():

    # output file name - if any
    ofilename=os.getenv('GLADE_HSM_RECALL_FILE_LIST')
    #print(ofilename)

    # append 'filepath' to ouptut file list.
    # keep the file descriptor open as an attribute like s static variable,
    # opening it once.
    # we don't open the file outright, it is possible there are no offline
    # files in the input list.  So we will open on-demand, only, if we
    # encounter an offline file
    def append_to_filelist(filepath):
        assert ofilename
        if not hasattr(append_to_filelist, 'recall_list'):
            #print('Opening File')
            append_to_filelist.recall_list = open(ofilename, 'wt')

        #print(f'Writing to {ofilename} file: '+filepath)
        append_to_filelist.recall_list.write(filepath + '\n')
        return

    # initialize counters
    n_total_files=0
    n_offline_files=0
    total_ondisk_bytes=0
    total_true_bytes=0

    for filepath in read_null_terminated_filenames(sys.stdin.buffer):

        try:
            file_ondisk_bytes, file_true_bytes = process_file(filepath)

        except Exception as e:
            print(f"Error processing file '{filepath}': {e}")
            continue

        total_ondisk_bytes += file_ondisk_bytes
        total_true_bytes += file_true_bytes

        n_total_files += 1
        is_offline = True if file_true_bytes > 100*file_ondisk_bytes else False

        if is_offline:
            n_offline_files += 1
            if ofilename:
                append_to_filelist(filepath)

        # incremental progress
        if 0 == n_total_files%100e3:
            print(' ... found {:,} files, {:,} offline, {} currently on disk, {} total.'.format(n_total_files,
                                                                                                n_offline_files,
                                                                                                human_readable_bytes(total_ondisk_bytes),
                                                                                                human_readable_bytes(total_true_bytes)))
    print('  Disk Volume:  {}'.format(human_readable_bytes(total_ondisk_bytes)))
    print('  Total Volume: {}'.format(human_readable_bytes(total_true_bytes)))
    print('  Total Files: {:,} / Offline: {:,}'.format(n_total_files,n_offline_files))

    # force flush before exit; otherwise no guarantee we will see the output
    # (observed on Derecho...)
    if hasattr(append_to_filelist, 'recall_list'): append_to_filelist.recall_list.flush()

    return



if __name__ == "__main__":
    main()
