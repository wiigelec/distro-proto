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
    if [[ ! -d "$stage/usr" || -L "$stage/usr" ]]; then
        echo "missing staged usr tree: $name" >&2
        exit 1
    fi
    shopt -s dotglob nullglob globstar
    count=0
    # Only leaf symlinks with same-directory targets are supported. They are
    # copied as links, never dereferenced during installation. A link may not
    # appear as the parent of another staged or installed entry.
    for item in "$stage"/**; do
        [[ "$item" == "$stage" ]] && continue
        relative=${item#"$stage"/}
        target="$destination/$relative"
        if [[ -L "$item" ]]; then
            link_target=$(/usr/bin/readlink -- "$item") || exit 1
            # A same-directory link cannot redirect through an absolute path
            # or a parent directory. Multi-component links require separately
            # audited root-relative resolution before they can be supported.
            if [[ -z "$link_target" || "$link_target" == /* ||
                  "$link_target" == . || "$link_target" == .. ||
                  "$link_target" == */* ]]; then
                echo "unsafe staged link: $relative" >&2
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
        if [[ "${item##*/}" != usr ]]; then
            echo "unexpected payload outside /usr" >&2
            exit 1
        fi
    done
    /usr/bin/cp -a -- "$stage"/. "$destination"/
    printf '%s\n' "$name" >> "$work/installed-packages"
done
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
