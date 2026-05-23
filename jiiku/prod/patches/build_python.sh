#!/bin/zsh

set -e

NUM_JOBS=$(nproc 2>/dev/null || sysctl -n hw.ncpu)
arg="$1"

# Make sure we are on main and update from remote
git restore MODULE.bazel
git fetch origin refs/heads/main:refs/remotes/origin/main
git checkout main
git pull origin main
git config --global --add safe.directory /app/sekai-modules

if [[ "$arg" == "skip" ]]; then
  skip_git=1
  commit=""
else
  skip_git=0
  commit="$arg"
fi
commit="$1"

# If a commit is provided, check it out
if (( skip_git )); then
  echo "Skipping checkout and submodule update for auto update"

elif [[ -n "$commit" ]]; then
  echo "Checking out commit: $commit"
  git checkout "$commit" || {
    echo "Error: Invalid commit $commit"
    exit 1
  }
  git submodule sync --recursive
  git submodule update --init --recursive --remote
else
  echo "Using latest commit on dev"
  git submodule sync --recursive
  git submodule update --init --recursive --remote
fi

# Update submodules
git submodule sync --recursive
git submodule update --init --recursive --remote

# Go to script root
root="${0:a:h}"
cd "$root"

# Ensure directory exists
mkdir -p data/storage-sekai-best

# Download music_metas.json
metas_path="data/storage-sekai-best/music_metas.json"
cp music_metas.json "$metas_path"

# Restore patched BUILD.bazel
cp /tmp/BUILD.bazel.patch data/BUILD.bazel

# Replace Jiiku's sekai modules, monkey patch modules with latest version.
git grep -lz 'common --registry https://raw.githubusercontent.com/Jiiku831/sekai-modules/main/' \
  | xargs -0 sed -i 's#common --registry https://raw.githubusercontent.com/Jiiku831/sekai-modules/main/#common --registry https://raw.githubusercontent.com/bemusicscript/sekai-modules/main/#g' | true
ver=$(wget -q -O - https://raw.githubusercontent.com/bemusicscript/sekai-modules/main/modules/sekai-master-db/latest)
sed -i "s|bazel_dep(name=\"sekai-master-db\", version=\"[^\"]*\")|bazel_dep(name=\"sekai-master-db\", version=\"$ver\")|" MODULE.bazel

build_opts=(
  -c opt
  --jobs="$NUM_JOBS"
  --spawn_strategy=local
  --cxxopt=-std=c++23
  --cxxopt="-pthread"
  --host_cxxopt=-std=c++23
  --copt="-O3"
  --copt="-Wno-nullability-completeness"
  --linkopt=-O3
  --linkopt="-pthread"
  --disk_cache=/app/bazel-cache
  --incompatible_strict_action_env
)

# Build the Python target (adjust if the query shows a different name)
# Or the bindings target like //sekai/run_analysis:bridge_py
bazelisk build "${build_opts[@]}" "//sekai/run_analysis/testing:analyze_main"
# bazelisk run //sekai/run_analysis/testing:analyze_main

# Move dereference symlinks and we all good..
OUT_FILES=($(bazelisk cquery "//sekai/run_analysis/testing:analyze_main" --output=files "${build_opts[@]}"))
if [ ${#OUT_FILES[@]} -gt 0 ]; then
  echo "Bundling ${#OUT_FILES[@]} targets..."
  tar -hc "${OUT_FILES[@]}" | tar -x -C ./dist/
fi

# Specifically grab the runfiles (the .so and .py files)
RUNFILES_DIR="bazel-bin/sekai/run_analysis/testing/analyze_main.runfiles"
if [ -d "$RUNFILES_DIR" ]; then
  echo "Streaming runfiles to ./dist..."
  tar -hc "$RUNFILES_DIR" | tar -x -C ./dist/
fi

# Shutdown bazelisk or server will hang forver
bazelisk shutdown

exit 0