{
  # Fallback values if hosts/<hostname>.nix is missing. The deliberately
  # invalid Forgejo identity makes MCT_I3A's student hook fail closed.
  gitName  = "Student";
  gitEmail = "student@example.invalid";
  forgejo  = "UNCONFIGURED";
}
