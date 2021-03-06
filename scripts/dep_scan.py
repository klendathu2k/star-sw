#!/usr/bin/env python3

"""

STAR libraries may include header files that are generated by STIC from *.idl
definitions from other libraries. Latter libraries need to be compiled before
former. This utility scans for such library interdependencies.

To do that one needs to maintain a list of such libraries that contain source
files. The source files may include other source files or generated header
files. Generated header files may include other generated headers, and they
belong to a specific library. We are looking to traverse a graph like this:

                              +----------+
                              |          |
                 +------------+   libs   <--------------------+
                 |            |          |                    |
                 |            +----------+                    |
      +----------v---------+                +-----------------+--------+
      |                    |                |                          |
  +---+   sources          +---------------->   generated headers      |
  |   |   *cxx, *.h, ...   |                |   *.h, St_*_Table.h,     +---+
  |   |                    |                |   St_*_Module.h, *.inc   |   |
  |   +----^---------------+                |                          |   |
  |        |                                +---------------------^----+   |
  +--------+                                                      |        |
                                                                  +--------+

"""

import os
import re
import sys

def read_blacklist():
    file_blacklist = os.path.join(os.path.dirname(__file__), '../cmake/blacklisted_lib_dirs.txt')
    with open(file_blacklist) as f:
        lib_blacklist = f.readlines()
    # Remove comments at the end of each line and surrounding whitespace
    lib_blacklist = [(re.sub("#.*$", "", x)).strip() for x in lib_blacklist]
    return lib_blacklist

lib_blacklist = set(read_blacklist())

def scan_libs(src_root, relpath):
    with os.scandir(os.path.join(src_root, relpath)) as scan:
        dirs = filter(lambda entry: entry.is_dir(), scan)
        libs = [_dir.name for _dir in dirs]
    libs = filter(lambda libname: os.path.join(relpath, libname) not in lib_blacklist, libs)
    return dict([(libname, os.path.join(relpath, libname)) for libname in libs])

def is_source_ext(filename):
    """
    Tells if filename (filepath) is a source file. For our purposes "sources"
    are any files that can #include and can be included.
    """
    _, ext = os.path.splitext(filename)
    return ext in [".h", ".hh", ".hpp", ".inc", ".c", ".cc", ".cxx", ".cpp", ".f", ".F"]

def is_idl_ext(filename):
    _, ext = os.path.splitext(filename)
    return ext in [".idl"]

include_regex = re.compile(r"""^\s*#\s*include\s+(<[^>"]+>|"[^>"]+")""")
def find_all_includes(lines):
    """
    Scan list of strings *lines* for #include preprocessor directives and yield
    included files

    >>> list(find_all_includes(["foo", '\t#include "path/to/test.h"', "#  include  <foo.h>", "// #include <notused>"]))
    ['path/to/test.h', 'foo.h']
    """
    for line in lines:
        m = include_regex.match(line)
        if m:
            include_path = m.group(1).strip('<"').rstrip('>"')
            yield include_path

def register_generated_headers(gen_header_to_lib, idl_name, libname):
    """
    Updates *gen_header_to_lib* to define that headers for *idl_name* are produced by *libname*
    """
    gen_header_to_lib.setdefault("{}.h".format(idl_name), set()).add(libname)
    gen_header_to_lib.setdefault("{}.inc".format(idl_name), set()).add(libname)
    # An idl file will produce either _Table or _Module, we don't really care which
    gen_header_to_lib.setdefault("St_{}_Table.h".format(idl_name), set()).add(libname)
    gen_header_to_lib.setdefault("St_{}_Module.h".format(idl_name), set()).add(libname)

def register_generated_headers_dependencies(source_to_source, idl_name, idl_path):
    """
    Scans idl file for #include statements. They don't have the usual C
    preprocessor meaning. For example,

      #include "ctg_geo.idl"

    in ctg.idl will translate to

      #include "ctg_geo.h"

    in ctg.h and to

      #include "ctg_geo.inc"

    in ctg.inc and to

      #include "tables/St_ctg_geo_Table.h"

    in St_ctg_Module.h

    This function ensures that these dependencies are respected by putting them
    in *source_to_source* map.
    """
    # Account for dependencies between generated files
    with open(idl_path, errors='replace') as fp:
        lines = fp.readlines()
    for include_path in find_all_includes(lines):
        _, include_filename = os.path.split(include_path)
        include_idl_name, _ = os.path.splitext(include_filename)
        # In principle these should have a separate
        # gen_header_to_gen_header map, but for simplicity lets just
        # add them to sources_to_sources
        source_to_source.setdefault("{}.h".format(idl_name), set()).add("{}.h".format(include_idl_name))
        source_to_source.setdefault("{}.inc".format(idl_name), set()).add("{}.inc".format(include_idl_name))
        source_to_source.setdefault("St_{}_Table.h".format(idl_name), set()).add("St_{}_Table.h".format(include_idl_name))
        # This is not a typo. _Module is mapped to _Table. Real stic
        # probably looks at the included file to determine what suffix
        # should be used. For our purposes we are not interested in
        # exact filename, we just construct a useful dependency graph.
        source_to_source.setdefault("St_{}_Module.h".format(idl_name), set()).add("St_{}_Table.h".format(include_idl_name))

def scan_sources(src_root, libname, librelpath, callback):
    """
    Scan *src_root*/*librelpath* directory.
    """
    libpath = os.path.join(src_root, librelpath)
    for dirpath, dirnames, filenames in os.walk(libpath):
        if libname == "sim_Tables":
            # sim_Tables should not look inside St_g2t
            St_g2t_path = os.path.join(src_root, "pams/sim/g2t")
            is_subpath = os.path.commonpath([dirpath, St_g2t_path]) == St_g2t_path
            if is_subpath:
                continue
            # XXX why star-cmake doesn't try to compile this as a part of sim_Tables?
            St_g2r_path = os.path.join(src_root, "pams/sim/g2r")
            is_subpath = os.path.commonpath([dirpath, St_g2r_path]) == St_g2r_path
            if is_subpath:
                continue
        for filename in filenames:
            filepath = os.path.join(dirpath, filename)
            callback(libname, filepath)

def recursive_find_dependencies(source_filename, source_to_source, gen_header_to_lib, visited=None):
    """
    Recursively traverse dependency graph given by *source_to_source* and
    *gen_header_to_lib* to find libraries that may be required for
    *source_filename*.
    """
    # this function can be sped up using memoization
    res = set()
    if visited is None:
        visited = set()
    visited.add(source_filename)
    if source_filename in gen_header_to_lib:
        res.update(gen_header_to_lib[source_filename])
    for other_include_filename in source_to_source.get(source_filename, []):
        if other_include_filename not in visited:
            res.update(recursive_find_dependencies(other_include_filename, source_to_source, gen_header_to_lib, visited))
    return res

def scan_star(src_root):
    lib_to_librelpath = {}
    lib_to_librelpath.update(scan_libs(src_root, "StRoot"))
    lib_to_librelpath.update(scan_libs(src_root, "StarVMC"))
    lib_to_librelpath.update({
        "StDb_Tables" : "StDb",
        "ctf_Tables" : "pams/ctf",
        "emc_Tables" : "pams/emc",
        "ftpc_Tables" : "pams/ftpc",
        "gen_Tables" : "pams/gen",
        "geometry_Tables" : "pams/geometry",
        "global_Tables" : "pams/global",
        "sim_Tables" : "pams/sim",
        "svt_Tables" : "pams/svt",
        "geometry_Tables" : "pams/geometry",
        "St_ctf" : "pams/ctf",
        "St_g2t" : "pams/sim/g2t",
        })

    # Mapping from library name to list of its source file paths
    lib_to_sources = {}
    # Mapping from generated header name to list of libraries that provide it
    gen_header_to_lib = {}
    # Mapping from source filename to its includes (that are also sources)
    source_to_source = {}

    def scan_callback(libname, filepath):
        """
        1. scan source and idl files for #include preprocessor directives to determine interdepenencies
        2. build a map from generated header name to list of libraries that produce such header
        """
        filename = os.path.basename(filepath)
        if is_source_ext(filename):
            lib_to_sources.setdefault(libname, []).append(filename)
            with open(filepath, errors='replace') as fp:
                lines = fp.readlines()
            for include_filepath in find_all_includes(lines):
                # We discard path to include and only look at the include filename.
                # This produces some false positives for dependencies.
                _, include_filename = os.path.split(include_filepath)
                source_to_source.setdefault(filename, set()).add(include_filename)
        elif is_idl_ext(filename):
            idl_name, _ = os.path.splitext(filename)
            register_generated_headers(gen_header_to_lib, idl_name, libname)
            register_generated_headers_dependencies(source_to_source, idl_name, filepath)

    for libname, librelpath in lib_to_librelpath.items():
        res = scan_sources(src_root, libname, librelpath, scan_callback)

    # Check if a generated header with the same name is provided by multiple libraries
    for gen_header, libs in gen_header_to_lib.items():
        ctf_only = all([libname in ["St_ctf", "ctf_Tables"] for libname in libs])
        if len(libs) > 1 and not ctf_only:
            print("Warning: Library mapping for {} is not unique: {}".format(gen_header, libs), file=sys.stderr)

    # A map of inter-library dependencies
    lib_dependencies = {}

    for libname, sources in lib_to_sources.items():
        for source_filename in sources:
            res = recursive_find_dependencies(source_filename, source_to_source, gen_header_to_lib)
            for other_libname in res:
                if other_libname != libname:
                    lib_dependencies.setdefault(libname, set()).add(other_libname)

    # Compiled for the same source code, not a dependency
    lib_dependencies["ctf_Tables"].remove("St_ctf")

    return lib_dependencies

if __name__ == "__main__":
    import doctest
    doctest.testmod(raise_on_error=True)

    import argparse
    parser = argparse.ArgumentParser(description="Scan STAR repository to determine dependencies to libraries with generated headers")
    parser.add_argument('src_root')
    args = parser.parse_args()

    lib_dependencies = scan_star(args.src_root)

    print("# This list is generated automatically. Do not edit!")
    for libname in sorted(lib_dependencies.keys()):
        dependencies = lib_dependencies[libname]
        print("add_dependencies({} {})".format(libname, " ".join(sorted(dependencies))))
