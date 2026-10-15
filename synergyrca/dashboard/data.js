window.KUBEMIND_DATA = [
  {
    "id": "FailedCreate-demo-es-cronjob",
    "time": "2026-10-11 12:18:49",
    "reason": "FailedCreate",
    "subtype": "ResourceQuota",
    "object": "Job/es-cronjob",
    "namespace": "demo",
    "status": "DIAGNOSED",
    "confidence": "HIGH",
    "method": "rules",
    "message": "Error creating: pods \"es-cronjob-xl9jv\" is forbidden: exceeded quota: compute-resources, requested: pods=1, used: pods=1, limited: pods=1",
    "rootCause": "ResourceQuota 'compute-resources' in namespace 'demo' has reached its pods limit (1/1).",
    "graph": [
      {
        "kind": "Job",
        "name": "es-cronjob",
        "root": false,
        "missing": false,
        "external": false
      },
      {
        "kind": "Namespace",
        "name": "demo",
        "root": false,
        "missing": false,
        "external": false
      },
      {
        "kind": "ResourceQuota",
        "name": "compute-resources",
        "root": true,
        "missing": false,
        "external": false
      }
    ],
    "evidence": [
      [
        "Namespace/demo",
        "status.phase",
        "Active"
      ],
      [
        "ResourceQuota/demo/compute-resources",
        "spec.hard.pods",
        "1"
      ],
      [
        "ResourceQuota/demo/compute-resources",
        "status.hard.pods",
        "1"
      ],
      [
        "ResourceQuota/demo/compute-resources",
        "status.used.pods",
        "1"
      ]
    ],
    "checks": [
      [
        "namespace demo phase is not Terminating (phase=Active)",
        true
      ],
      [
        "used.pods >= hard.pods (1 >= 1)",
        false
      ]
    ],
    "fix": [
      "kubectl patch resourcequota compute-resources -n demo -p '{\"spec\":{\"hard\":{\"pods\":\"60\"}}}' --dry-run=server"
    ],
    "logTail": [],
    "dryRun": "resourcequota/compute-resources patched"
  }
];
