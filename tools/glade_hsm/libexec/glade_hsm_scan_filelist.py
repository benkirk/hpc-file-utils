#!/usr/bin/env python3


import sys, os, stat, time

GPFS_BLOCKSIZE=int(os.getenv('GPFS_BLOCKSIZE', 512))
HSM_THRESHOLD_SIZE_CHECK=int(os.getenv('HSM_THRESHOLD_SIZE_CHECK', 100*1024*1024-10)) # <-- ~100MiB with margin


# a python class with a write method that contains an output file object,
# and only opens the output file the first time the write method is called
class LazyWriter:
    def __init__(self, filename, mode="wt", encoding="utf-8"):
        self.filename = filename
        self.mode = mode
        self.encoding = encoding
        self._file = None   # File handle will be opened lazily

    def write(self, data):
        if self._file is None:
            # Open the file the first time write() is called
            self._file = open(self.filename, self.mode, encoding=self.encoding)
        self._file.write(data)

    def flush(self):
        if self._file is not None:
            self._file.flush()

    def close(self):
        if self._file is not None:
            self._file.close()
            self._file = None

    def __del__(self):
        # Make sure file is closed when object is destroyed
        self.close()



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



def get_file_status(filepath):

    #print(f"Processing: {filepath}")

    stat_info = os.stat(filepath)

    # decode file type - should be a regular file only; (output from find -type f)
    fmode = stat_info.st_mode

    assert stat.S_ISREG(fmode)

    # about stat_info.st_size: we can't always trust it.
    # for sparse files, or files migrated to take via HSM,
    # the size is not what is occupied on disk.
    # a better metric is (number of blocks * fs_block_size);

    # for regular, uncompressed, unmigrated files; size <= (n_blocks*fs_block_size)
    file_true_size = stat_info.st_size
    file_nblocks = stat_info.st_blocks
    file_ondisk_size = min(file_true_size, file_nblocks*GPFS_BLOCKSIZE)

    is_offline = False
    # we only potentially migrate files > 100MiB,
    if file_true_size > HSM_THRESHOLD_SIZE_CHECK:
        is_offline = True if file_true_size > 100*file_ondisk_size else False

    return file_ondisk_size, file_true_size, is_offline



def main():

    # output file name - if any
    ofilename=os.getenv('GLADE_HSM_RECALL_FILE_LIST')
    #print(ofilename)

    offline_list = LazyWriter(ofilename)

    # initialize counters
    n_total_files=0
    n_offline_files=0
    n_candidate_files=0
    total_ondisk_bytes=0
    total_true_bytes=0
    total_candidate_bytes=0

    for filepath in read_null_terminated_filenames(sys.stdin.buffer):

        try:
            file_ondisk_bytes, file_true_bytes, is_offline = get_file_status(filepath)

        except Exception as e:
            print(f"Error processing file '{filepath}': {e}")
            continue

        total_ondisk_bytes += file_ondisk_bytes
        total_true_bytes += file_true_bytes

        n_total_files += 1

        if is_offline:
            n_offline_files += 1
            if ofilename:
                offline_list.write(filepath + '\n')

        if file_true_bytes > HSM_THRESHOLD_SIZE_CHECK and not is_offline:
            n_candidate_files += 1
            total_candidate_bytes += file_ondisk_bytes

        # incremental progress
        if 0 == n_total_files%100e3:
            print(' ... found {:,} files, {:,} offline, {} currently on disk, {} total.'.format(n_total_files,
                                                                                                n_offline_files,
                                                                                                human_readable_bytes(total_ondisk_bytes),
                                                                                                human_readable_bytes(total_true_bytes)))
    if n_offline_files > 0:
        print('  Total Files: {:,} / Offline: {:,}'.format(n_total_files,n_offline_files))
    else:
        print('  Total Files: {:,}'.format(n_total_files))

    print('  Total Data:  {} On Disk, {} True Size'.format(human_readable_bytes(total_ondisk_bytes),
                                                           human_readable_bytes(total_true_bytes)))
    if 0 != n_candidate_files:
        print('  HSM Candidates: {:,} file{}, {}'.format(n_candidate_files,
                                                         's' if n_candidate_files >1 else '',
                                                         human_readable_bytes(total_candidate_bytes)))
    if ofilename:
        if n_offline_files != 0:
            print('  Recall Volume: {}'.format(human_readable_bytes(total_true_bytes - total_ondisk_bytes)))
    return



if __name__ == "__main__":
    main()
