{ config, pkgs, lib, inputs, ... }:

{
  programs.git = {
    enable = true;
    package = pkgs.gitFull.override { withLibsecret = true; };

    settings = {
      credential.helper = "libsecret";
      core.askPass = "";
      init.defaultBranch = "master";

      # Classroom-safe defaults: a pull may fast-forward, but it must never
      # create a merge commit or silently start a rebase.
      pull.rebase = false;
      pull.ff = "only";
      rebase.autoStash = false;
      fetch.prune = true;
      rerere.enabled = true;
      merge.ff = "only";

      # Repacking on Windows/SMB network drives has caused file-locking
      # problems in the past. Do maintenance explicitly instead of letting
      # normal Git commands trigger it automatically.
      gc.auto = 0;
      maintenance.auto = false;

      alias = {
        st = "status -sb";
        lg = "log --oneline --graph --decorate --all";
        br = "branch -vv";

        pub = ''
          !f(){
            set -e;
            b=$(git rev-parse --abbrev-ref HEAD);
            git push -u origin "$b";
          }; f
        '';

        # Generic update helper. Unlike the MCT-specific upmaster, this only
        # rebases onto the configured upstream. Local commits require an
        # explicit --force/-f. There is deliberately no autostash.
        up = ''
          !f(){
            set -e;
            force=0;
            if [ "$1" = "--force" ] || [ "$1" = "-f" ]; then
              force=1;
              shift;
            fi;

            u=$(git rev-parse --abbrev-ref --symbolic-full-name @{u} 2>/dev/null) || {
              echo "ERROR: No upstream configured. Run: git pub";
              exit 1;
            };

            if [ -n "$(git status --porcelain)" ]; then
              echo "ERROR: Working tree is not clean. Commit or discard changes first.";
              exit 1;
            fi;

            git fetch --prune;
            ahead=$(git rev-list --count @{u}..HEAD);

            if [ "$ahead" -gt 0 ] && [ "$force" -eq 0 ]; then
              echo "ERROR: You have $ahead local commit(s) that are NOT pushed.";
              echo "       Run: git pub   or (explicitly): git up --force";
              exit 1;
            fi;

            if [ "$ahead" -gt 0 ] && [ "$force" -eq 1 ]; then
              b=$(git rev-parse --abbrev-ref HEAD);
              echo "WARNING: git up --force";
              echo "  Branch:   $b";
              echo "  Upstream: $u";
              echo "  Status:   ahead by $ahead commit(s) (not pushed)";
              echo "  Action:   rebase onto upstream";
              echo "  Note:     If you push afterwards, you may need --force-with-lease.";
              echo "            To abort a conflict: git abort-op";
              echo "";
            fi;

            git rebase @{u};
          }; f
        '';

        # During a rebase Git's ours/theirs terminology is counter-intuitive.
        # These helpers deliberately expose the human meaning instead.
        current = ''
          !f(){
            if test -d "$(git rev-parse --git-path rebase-apply)" -o -d "$(git rev-parse --git-path rebase-merge)"; then
              git checkout --theirs -- "$@";
            else
              git checkout --ours -- "$@";
            fi;
          }; f
        '';
        incoming = ''
          !f(){
            if test -d "$(git rev-parse --git-path rebase-apply)" -o -d "$(git rev-parse --git-path rebase-merge)"; then
              git checkout --ours -- "$@";
            else
              git checkout --theirs -- "$@";
            fi;
          }; f
        '';

        undo = "reset --soft HEAD~1";
        discard = "reset --hard";

        # Fast classroom recovery: make the local branch identical to its
        # remote branch. Untracked files are kept unless --clean is explicit.
        reset-to-remote = ''
          !f(){
            set -e;
            clean=0;
            b="";

            for arg in "$@"; do
              case "$arg" in
                --clean) clean=1 ;;
                -*) echo "ERROR: Unknown option: $arg"; exit 2 ;;
                *)
                  if [ -n "$b" ]; then
                    echo "ERROR: Only one branch may be specified.";
                    exit 2;
                  fi;
                  b="$arg";
                  ;;
              esac;
            done;

            # A half-finished rebase/merge is usually the reason this command
            # is needed. Abort it first so HEAD is back on a real branch.
            git abort-op >/dev/null 2>&1 || true;

            if [ -z "$b" ]; then
              b=$(git rev-parse --abbrev-ref HEAD);
            fi;
            if [ "$b" = master ] || [ "$b" = main ]; then
              echo "ERROR: reset-to-remote nicht auf '$b' ausfuehren.";
              exit 1;
            fi;

            git fetch --all --prune;
            git switch "$b";

            if git show-ref --verify --quiet "refs/remotes/origin/$b"; then
              r="origin/$b";
            else
              u=$(git rev-parse --abbrev-ref --symbolic-full-name @{u} 2>/dev/null || true);
              if [ -n "$u" ]; then
                r="$u";
              else
                echo "ERROR: Weder origin/$b noch upstream gefunden.";
                exit 1;
              fi;
            fi;

            git reset --hard "$r";
            if [ "$clean" -eq 1 ]; then
              echo "WARNING: deleting untracked files/directories (git clean -fd)";
              git clean -fd;
            fi;
          }; f
        '';

        abort-op = ''
          !f(){
            set +e;
            try(){
              op="$1";
              shift;
              "$@" >/dev/null 2>&1;
              rc=$?;
              if [ $rc -eq 0 ]; then
                echo "OK: aborted $op";
                exit 0;
              fi;
            };

            try rebase      git rebase --abort;
            try merge       git merge --abort;
            try cherry-pick git cherry-pick --abort;
            try revert      git revert --abort;
            try am          git am --abort;
            try bisect      git bisect reset;
            echo "INFO: no in-progress operation detected.";
          }; f
        '';

        # Read-only first aid: one command gives the teacher the state that is
        # normally collected with several Git commands at a student's PC.
        doctor = ''
          !f(){
            set +e;
            top=$(git rev-parse --show-toplevel 2>/dev/null) || {
              echo "ERROR: not inside a Git repository.";
              exit 1;
            };
            b=$(git rev-parse --abbrev-ref HEAD 2>/dev/null);
            u=$(git rev-parse --abbrev-ref --symbolic-full-name @{u} 2>/dev/null);
            student=$(git config --get mct.student 2>/dev/null);

            echo "Repository:  $top";
            echo "Branch:      $b";
            echo "Upstream:    ''${u:-<none>}";
            echo "mct.student: ''${student:-<unset>}";

            if [ -n "$u" ]; then
              behind=$(git rev-list --count HEAD..@{u} 2>/dev/null);
              ahead=$(git rev-list --count @{u}..HEAD 2>/dev/null);
              echo "Ahead/behind: ''${ahead:-?}/''${behind:-?}";
            fi;

            op="none";
            test -d "$(git rev-parse --git-path rebase-merge 2>/dev/null)" -o -d "$(git rev-parse --git-path rebase-apply 2>/dev/null)" && op="rebase";
            test -f "$(git rev-parse --git-path MERGE_HEAD 2>/dev/null)" && op="merge";
            test -f "$(git rev-parse --git-path CHERRY_PICK_HEAD 2>/dev/null)" && op="cherry-pick";
            echo "Operation:   $op";
            echo "";
            git status -sb;
            echo "";
            echo "Remotes:";
            git remote -v;
            echo "";
            echo "Recent commits:";
            git log --oneline --graph --decorate -8;
          }; f
        '';
      };
    };
  };
}
