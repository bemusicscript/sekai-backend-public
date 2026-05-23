#!/usr/bin/zsh

set -e

root="${0:a:h}"
cd "$root"

version_regex='([0-9]\.?)+'

function confirm() {
    read -q "REPLY?Commit? [y/N] "
    echo ""
    if [[ ! $REPLY =~ ^[Yy]$ ]]
    then
        [[ "$0" = "$BASH_SOURCE" ]] && exit 1 || return 1
    fi
}

function update () {
  local remote=$1
  local suffix=$2
  local name="$(basename $remote | cut -d. -f1)"

  echo "Updating: $name"

  if ! [[ -d "$root/scratch/$name" ]]; then
    mkdir -p "$root/scratch"
    git clone "$remote" "$root/scratch/$name"
  fi

  pushd "$root/scratch/$name"
  git pull

  local version=$(git cat-file commit "$(git show-ref -s HEAD)" \
    | grep -oE "master version $version_regex" \
    | grep -oE "$version_regex")

  if [[ -z "$version" ]]; then
    echo "ERROR: version not found"
    return 1
  fi

  local count=$(git rev-list --grep="master version $version" --count HEAD)

  local rev="$(git show-ref -s HEAD)"
  local full_suffix=""
  local dash_suffix=""
  if [[ -n "$suffix" ]]; then
    full_suffix=".$suffix"
    dash_suffix="-$suffix"
  fi
  local fq_version="$version.$count$full_suffix"
  echo "Master version: $fq_version ($rev)"
  local metadata_path="$root/modules/sekai-master-db/metadata.json"
  local out_dir="$root/modules/sekai-master-db/$fq_version"

  popd

  if [[ "$(jq --arg v "$fq_version" '.versions | index($v)' "$metadata_path")" != "null" ]]; then
    echo "SKIP: already up to date"
    return 0
  fi

  mkdir -p "$root/scratch/archive"
  local zip_out="$root/scratch/archive/$fq_version.zip"
  wget -nc -O "$zip_out" "${remote::-4}/archive/$rev.zip" || true
  local hash="$(cat "$zip_out" | openssl dgst -sha384 -binary | openssl base64 -A)"
  local integrity="sha384-$hash"
  echo "Computed integrity: $integrity"

  mkdir -p "$out_dir/patches"
  local latest_symlink="$root/modules/sekai-master-db/latest$dash_suffix"
  rsync -a "$latest_symlink/patches" "$out_dir"

  cat << EOF > "$out_dir/MODULE.bazel"
module(
    name = "sekai-master-db",
    version = "$fq_version",
)
EOF

  cat << EOF > "$out_dir/source.json"
{
    "integrity": "$integrity",
    "strip_prefix": "$name-$rev",
    "url": "${remote::-4}/archive/$rev.zip",
    "patch_strip": 0,
    "patches": {
      "add_build_file.patch": "sha384-$(cat "$out_dir/patches/add_build_file.patch" | openssl dgst -sha384 -binary | openssl base64 -A)",
      "add_module_file.patch": "sha384-$(cat "$out_dir/patches/add_module_file.patch" | openssl dgst -sha384 -binary | openssl base64 -A)",
      "server_id.patch": "sha384-$(cat "$out_dir/patches/server_id.patch" | openssl dgst -sha384 -binary | openssl base64 -A)"
    }
}
EOF

  local tmp=$(mktemp)
  jq --indent 4 --arg v "$fq_version" \
    '.versions |= . + [$v] | .versions |= sort' "$metadata_path" > "$tmp"
  mv "$tmp" "$metadata_path"

  local latest_path="$root/modules/sekai-master-db/latest$full_suffix.txt"
  echo "$fq_version" > "$latest_path"

  rm "$latest_symlink"
  pushd "$root/modules/sekai-master-db/"
  ln -s "$fq_version" "$latest_symlink"
  popd

  git add "$out_dir" "$metadata_path" "$latest_path" "$latest_symlink"
  git --no-pager diff --staged "$metadata_path"
  git --no-pager status --short | grep '^[MARCD]'
  confirm
  git commit -m "add master db version $fq_version"
}

update https://github.com/Sekai-World/sekai-master-db-diff.git
update https://github.com/Sekai-World/sekai-master-db-en-diff.git en
update https://github.com/Sekai-World/sekai-master-db-kr-diff.git kr
update https://github.com/Sekai-World/sekai-master-db-tc-diff.git tw
