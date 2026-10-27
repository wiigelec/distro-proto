#!/usr/bin/bash
# Assemble a fresh root from native package build procedures.
# Must execute with generation-A userspace, inside an isolated build namespace.
# Inputs: ordered plan (name<TAB>absolute-script), fresh root, workspace.
# The prepared build scripts must install to $workspace/$name/stage.
set -euo pipefail
umask 022
export PATH=/usr/bin:/bin LC_ALL=C
if (( $# != 3 )); then
    echo "usage: native_assemble.sh plan.tsv fresh-root workdir" >&2
    exit 2
fi
plan=$1
destination=$2
work=$3
if [[ ! -f "$plan" || -L "$plan" || -e "$destination" || -L "$destination" ||
      -e "$work" || -L "$work" ]]; then
    echo 'plan missing or output paths not fresh' >&2
    exit 1
fi
for path in "$destination" "$work"; do
    if [[ "$path" != /* || "$path" == / || "$path" == /usr ||
          "$path" == /bin || "$path" == /etc ]]; then
        echo 'unsafe output path' >&2
        exit 1
    fi
done
if [[ "$destination" == "$work" || "$destination" == "$work/"* ||
      "$work" == "$destination/"* ]]; then
    echo 'overlapping output paths' >&2
    exit 1
fi
declare -A seen=()
declare -a names=()
declare -a scripts=()
while IFS=$'\t' read -r name recipe extra || [[ -n "${name:-}" ]]; do
    if [[ -z "$name" || ! "$name" =~ ^[a-z0-9][a-z0-9+._-]*$ ||
          -n "${seen[$name]:-}" || -z "$recipe" || "$recipe" != /* ||
          ! -f "$recipe" || -L "$recipe" || -n "${extra:-}" ]]; then
        echo 'invalid package plan entry' >&2
        exit 1
    fi
    seen[$name]=1
    names+=("$name")
    scripts+=("$recipe")
done < "$plan"
if (( ${#names[@]} == 0 )); then
    echo 'empty package plan' >&2
    exit 1
fi
mkdir -m 0700 -- "$destination" "$work"
printf 'incomplete\n' > "$work/construction.status"
# Package build dispatch and package installation are both done by this
# native process, never by a host loop of per-package subprocesses.
for (( i=0; i<${#names[@]}; i++ )); do
    name=${names[i]}
    script=${scripts[i]}
    package_work="$work/$name"
    mkdir -m 0700 "$package_work"
    mkdir -m 0700 "$package_work/stage"
    # Each script receives its own stage path and must use it as DESTDIR.
    if ! DISTRO_STAGE="$package_work/stage"          DISTRO_WORK="$package_work"          /usr/bin/bash --noprofile --norc "$script"          >"$package_work/build.log" 2>&1; then
        echo "native build failed: $name" >&2
        exit 1
    fi
    stage="$package_work/stage"
    # Combine legacy /sbin and canonical /usr/sbin within the scratch stage.
    # Preflight every path: directory overlap is fine, but never overwrite a
    # file, symlink, or non-directory. Do not dereference symlink ancestors.
    if [[ -e "$stage/sbin" || -L "$stage/sbin" ]]; then
        if [[ -L "$stage/sbin" || ! -d "$stage/sbin" ||
              -L "$stage/usr" || ! -d "$stage/usr" ||
              -L "$stage/usr/sbin" ||
              ( -e "$stage/usr/sbin" && ! -d "$stage/usr/sbin" ) ]]; then
            echo "unsafe staged /sbin ownership: $name" >&2
            exit 1
        fi
        if [[ ! -e "$stage/usr/sbin" ]]; then
            /usr/bin/mv -- "$stage/sbin" "$stage/usr/sbin"
        else
            shopt -s dotglob nullglob globstar
            for item in "$stage/sbin"/**; do
                rel=${item#"$stage/sbin"/}
                target="$stage/usr/sbin/$rel"
                parent=${target%/*}
                while [[ "$parent" != "$stage/usr/sbin" ]]; do
                    if [[ -L "$parent" || ( -e "$parent" && ! -d "$parent" ) ]]; then
                        echo "unsafe staged /usr/sbin ancestor: $rel" >&2
                        exit 1
                    fi
                    parent=${parent%/*}
                done
                if [[ -d "$item" && ! -L "$item" ]]; then
                    if [[ -L "$target" || ( -e "$target" && ! -d "$target" ) ]]; then
                        echo "staged /sbin directory conflict: $rel" >&2
                        exit 1
                    fi
                elif [[ -f "$item" || -L "$item" ]]; then
                    if [[ -e "$target" || -L "$target" ]]; then
                        echo "staged /sbin file collision: $rel" >&2
                        exit 1
                    fi
                else
                    echo "unsupported staged /sbin entry: $rel" >&2
                    exit 1
                fi
            done
            # All collisions have been checked. Move leaves rather than
            # copy over existing paths; preserve symlinks without following.
            for item in "$stage/sbin"/**; do
                rel=${item#"$stage/sbin"/}
                target="$stage/usr/sbin/$rel"
                if [[ -d "$item" && ! -L "$item" ]]; then
                    /usr/bin/mkdir -p -- "$target"
                else
                    /usr/bin/mv -- "$item" "$target"
                fi
            done
            /usr/bin/find "$stage/sbin" -depth -type d -exec /usr/bin/rmdir -- {} +
        fi
    fi
    if [[ ! -d "$stage/usr" || -L "$stage/usr" ]]; then
        echo "missing staged usr tree: $name" >&2
        exit 1
    fi
    shopt -s dotglob nullglob globstar
    count=0
    # Staged links may use relative paths, but must resolve within the fresh
    # destination root. Never dereference a link while copying its payload.
    # A link may not be the parent of another staged or installed entry.
    for item in "$stage"/**; do
        [[ "$item" == "$stage" ]] && continue
        relative=${item#"$stage"/}
        target="$destination/$relative"
        if [[ -L "$item" ]]; then
            link_target=$(/usr/bin/readlink -- "$item") || exit 1
            if [[ -z "$link_target" || "$link_target" == /* ]]; then
                echo "unsafe staged link: $relative" >&2
                exit 1
            fi
            # realpath -m canonicalizes even dangling destinations. Treat
            # the proposed link relative to its eventual destination rather
            # than the temporary staging area. Prefix comparison is bounded
            # with '/' so similarly named sibling roots cannot be accepted.
            link_destination=$(/usr/bin/realpath -m -- "${target%/*}/$link_target") || exit 1
            if [[ "$link_destination" != "$destination/"* ||
                  "$link_destination" == "$target" ]]; then
                echo "staged link escapes native root or loops: $relative" >&2
                exit 1
            fi
        elif [[ ! -d "$item" && ! -f "$item" ]]; then
            echo "unsupported staged object: $relative" >&2
            exit 1
        fi
        source_ancestor=${item%/*}
        while [[ "$source_ancestor" != "$stage" ]]; do
            if [[ -L "$source_ancestor" ]]; then
                echo "staged link used as directory: $relative" >&2
                exit 1
            fi
            source_ancestor=${source_ancestor%/*}
        done
        ancestor=${target%/*}
        while [[ "$ancestor" != "$destination" ]]; do
            if [[ -L "$ancestor" || ( -e "$ancestor" && ! -d "$ancestor" ) ]]; then
                echo "unsafe destination ancestor: $relative" >&2
                exit 1
            fi
            ancestor=${ancestor%/*}
        done
        if [[ -d "$item" && ! -L "$item" ]]; then
            if [[ -e "$target" && ! -d "$target" ]]; then
                echo "directory collision: $relative" >&2
                exit 1
            fi
        else
            ((count+=1))
            if [[ -e "$target" || -L "$target" ]]; then
                echo "owned-path collision: $relative" >&2
                exit 1
            fi
        fi
    done
    if (( count == 0 )); then
        echo "empty package payload: $name" >&2
        exit 1
    fi
    for item in "$stage"/*; do
        if [[ "${item##*/}" != usr && "${item##*/}" != etc ]]; then
            echo "unexpected payload outside supported roots" >&2
            exit 1
        fi
    done
    /usr/bin/cp -a -- "$stage"/. "$destination"/
    printf '%s\n' "$name" >> "$work/installed-packages"
done
# Construct aliases only after native package assembly. These paths must
# never replace a package-owned file or directory.
if [[ -e "$destination/sbin" || -L "$destination/sbin" ]]; then
    echo 'native root contains conflicting /sbin' >&2
    exit 1
fi
if [[ ! -d "$destination/usr/sbin" || -L "$destination/usr/sbin" ]]; then
    echo 'native root missing /usr/sbin' >&2
    exit 1
fi
/usr/bin/ln -s usr/sbin "$destination/sbin"
# Construct the merged-/usr runtime lookup path from the native package stage.
# The linker and libraries must still be provided by declared package builds.
if [[ -e "$destination/lib64" || -L "$destination/lib64" ]]; then
    echo 'unexpected pre-existing native runtime link' >&2
    exit 1
fi
/usr/bin/ln -s usr/lib64 "$destination/lib64"
# Do not report construction completion if the new root cannot load its
# own native executables. The source of these components must be part of the
# declared native reconstruction rather than an implicit host or producer copy.
if [[ ! -L "$destination/lib64" ||
      $(/usr/bin/readlink -- "$destination/lib64") != usr/lib64 ||
      ! -f "$destination/usr/lib64/ld-linux-x86-64.so.2" ||
      ! -f "$destination/usr/lib64/libc.so.6" ||
      ! -f "$destination/usr/bin/bash" ]]; then
    echo 'native root missing declared runtime closure or filesystem links' >&2
    exit 1
fi
printf 'assembled-unverified\n' > "$work/construction.status"
echo 'Native root assembled; runtime and closure verification still required.'
