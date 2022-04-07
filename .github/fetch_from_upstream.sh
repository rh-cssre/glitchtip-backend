UPSTREAM_REPO_URL="https://gitlab.com/glitchtip/glitchtip-backend.git"
UPSTREAM_SYNC_BRANCH="master"
INPUT_TARGET_SYNC_BRANCH="master"
INPUT_UPSTREAM_SYNC_BRANCH="master"
HAS_NEW_COMMITS=true

git config user.name "GH Action - Upstream Sync"
git config user.email "action@github.com"
git config pull.rebase false

echo $INPUT_TARGET_SYNC_BRANCH

exit_no_commits() {
    echo 'No new commits to sync. Finishing sync action gracefully.'
}

find_last_synced_commit() {
    LAST_SYNCED_COMMIT=""
    TARGET_BRANCH_LOG="$(git rev-list "${INPUT_TARGET_SYNC_BRANCH}")"
    UPSTREAM_BRANCH_LOG="$(git rev-list "upstream/${INPUT_UPSTREAM_SYNC_BRANCH}")"

    for hash in ${TARGET_BRANCH_LOG}; do
        UPSTREAM_CHECK="$(echo "${UPSTREAM_BRANCH_LOG}" | grep "${hash}")"
        if [ -n "${UPSTREAM_CHECK}" ]; then
            LAST_SYNCED_COMMIT="${hash}"
            break
        fi
    done
}

# display new commits since last sync
output_new_commit_list() {
    if [ -z "${LAST_SYNCED_COMMIT}" ]; then
        echo "\nNo previous sync found from upstream repo. Syncing entire commit history."
        UNSHALLOW=true
    else
        echo '\nNew commits since last sync:'
        git log upstream/"${INPUT_UPSTREAM_SYNC_BRANCH}" "${LAST_SYNCED_COMMIT}"..HEAD
    fi
}

# create new branch and add new commits to it
add_branch_with_new_commits() {
    echo '\nSyncing new commits...'

    # pull_args examples: "--ff-only", "--tags", "--ff-only --tags"

    git checkout -b upstream-changes-`date +%Y-%m-%d` # create a new branch
    git pull upstream "${INPUT_UPSTREAM_SYNC_BRANCH}" --allow-unrelated-histories -X theirs
    git push -u origin upstream-changes-`date +%Y-%m-%d`

    git checkout master

    COMMAND_STATUS=$?

    if [ "${COMMAND_STATUS}" != 0 ]; then
        # exit on commit pull fail
        echo "New commits could not be pulled."
    fi

    echo 'SUCCESS\n'
}

# set upstream repo url
git remote add upstream $UPSTREAM_REPO_URL

# fetch commits from upstream branch within given time frame (default 1 month)
git fetch --quiet upstream "${INPUT_UPSTREAM_SYNC_BRANCH}"
COMMAND_STATUS=$?

if [ "${COMMAND_STATUS}" != 0 ]; then
    # if shallow fetch fails, no new commits are avilable for sync
    HAS_NEW_COMMITS=false
    exit_no_commits
fi

# output 'has_new_commits' value to workflow environment
echo "::set-output name=has_new_commits::${HAS_NEW_COMMITS}"

add_branch_with_new_commits