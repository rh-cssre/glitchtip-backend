large_event = {
  "sdk": {
    "name": "sentry.javascript.node",
    "version": "6.13.3",
    "packages": [{ "name": "npm:@sentry/node", "version": "6.13.3" }],
    "integrations": [
      "InboundFilters",
      "FunctionToString",
      "Console",
      "Http",
      "OnUncaughtException",
      "OnUnhandledRejection",
      "LinkedErrors"
    ]
  },
  "type": "error",
  "user": { "ip_address": "54.174.129.0" },
  "title": "TypeError: Cannot read property 'id' of undefined",
  "culprit": "None.<anonymous>(dist.webapp-frontend.server:535)",
  "message": "",
  "modules": "",
  "request": {},
  "metadata": {
    "type": "TypeError",
    "value": "Cannot read property 'id' of undefined",
    "filename": "/usr/src/app/dist/webapp-frontend/server/535.js",
    "function": "None.<anonymous>"
  },
  "platform": "node",
  "exception": {
    "values": [
      {
        "type": "TypeError",
        "value": "Cannot read property 'id' of undefined",
        "mechanism": { "type": "generic", "handled": True },
        "stacktrace": {
          "frames": [
            {
              "colno": 4444567,
              "in_app": False,
              "lineno": 209,
              "module": "dist.webapp-frontend.server:main",
              "filename": "/usr/src/app/dist/webapp-frontend/server/main.js",
              "function": "MapSubscriber._next",
              "pre_context": [
                "      }",
                "      pickaverb {",
                "        ...pickaverbState",
                "      }",
                "    }",
                "  }",
                "  ${pickaverbStateFragmentDoc}"
              ],
              "context_line": "'a long string of contexta long string of contexta long string of contexta long string of contexta long string of contexta long string of contexta long string of",
              "post_context": []
            },
            {
              "colno": 444456,
              "in_app": False,
              "lineno": 209,
              "module": "dist.webapp-frontend.server:main",
              "filename": "/usr/src/app/dist/webapp-frontend/server/main.js",
              "function": "MapSubscriber.project",
              "pre_context": [
                "      }",
                "      pickaverb {",
                "        ...pickaverbState",
                "      }",
                "    }",
                "  }",
                "  ${pickaverbStateFragmentDoc}"
              ],
              "context_line": "'a long string of contexta long string of contexta long string of contexta long string of contexta long string of contexta long string of contexta long string of",
              "post_context": []
            },
            {
              "colno": 444456,
              "in_app": False,
              "lineno": 209,
              "module": "dist.webapp-frontend.server:main",
              "filename": "/usr/src/app/dist/webapp-frontend/server/main.js",
              "function": "memoized",
              "pre_context": [
                "      }",
                "      pickaverb {",
                "        ...pickaverbState",
                "      }",
                "    }",
                "  }",
                "  ${pickaverbStateFragmentDoc}"
              ],
              "context_line": "'a long string of contexta long string of contexta long string of contexta long string of contexta long string of contexta long string of contexta long string of",
              "post_context": []
            },
            {
              "colno": 212679,
              "in_app": False,
              "lineno": 209,
              "module": "dist.webapp-frontend.server:main",
              "filename": "/usr/src/app/dist/webapp-frontend/server/main.js",
              "function": "None.<anonymous>",
              "pre_context": [
                "      }",
                "      pickaverb {",
                "        ...pickaverbState",
                "      }",
                "    }",
                "  }",
                "  ${pickaverbStateFragmentDoc}"
              ],
              "context_line": "'a long string of contexta long string of contexta long string of contexta long string of contexta long string of contexta long string of contexta long string of",
              "post_context": []
            },
            {
              "colno": 212009,
              "in_app": False,
              "lineno": 209,
              "module": "dist.webapp-frontend.server:main",
              "filename": "/usr/src/app/dist/webapp-frontend/server/main.js",
              "function": "defaultStateFn",
              "pre_context": [
                "      }",
                "      pickaverb {",
                "        ...pickaverbState",
                "      }",
                "    }",
                "  }",
                "  ${pickaverbStateFragmentDoc}"
              ],
              "context_line": "'a long string of contexta long string of contexta long string of contexta long string of contexta long string of contexta long string of contexta long string of",
              "post_context": []
            },
            {
              "colno": 444456,
              "in_app": False,
              "lineno": 209,
              "module": "dist.webapp-frontend.server:main",
              "filename": "/usr/src/app/dist/webapp-frontend/server/main.js",
              "function": "memoized",
              "pre_context": [
                "      }",
                "      pickaverb {",
                "        ...pickaverbState",
                "      }",
                "    }",
                "  }",
                "  ${pickaverbStateFragmentDoc}"
              ],
              "context_line": "'a long string of contexta long string of contexta long string of contexta long string of contexta long string of contexta long string of contexta long string of",
              "post_context": []
            },
            {
              "colno": 444455,
              "in_app": False,
              "lineno": 209,
              "module": "dist.webapp-frontend.server:main",
              "filename": "/usr/src/app/dist/webapp-frontend/server/main.js",
              "function": "None.<anonymous>",
              "pre_context": [
                "      }",
                "      pickaverb {",
                "        ...pickaverbState",
                "      }",
                "    }",
                "  }",
                "  ${pickaverbStateFragmentDoc}"
              ],
              "context_line": "'a long string of contexta long string of contexta long string of contexta long string of contexta long string of contexta long string of contexta long string of",
              "post_context": []
            },
            {
              "colno": 4444,
              "in_app": False,
              "lineno": 1,
              "module": "dist.webapp-frontend.server:535",
              "filename": "/usr/src/app/dist/webapp-frontend/server/535.js",
              "function": "None.<anonymous>",
              "pre_context": [],
              "context_line": "'a long string of contexta long string of contexta long string of contexta long string of contexta long string of contexta long string of contexta long string of",
              "post_context": []
            },
            {
              "colno": None,
              "in_app": False,
              "lineno": None,
              "filename": "",
              "function": "Array.find"
            },
            {
              "colno": 4444,
              "in_app": False,
              "lineno": 1,
              "module": "dist.webapp-frontend.server:535",
              "filename": "/usr/src/app/dist/webapp-frontend/server/535.js",
              "function": "None.<anonymous>",
              "pre_context": [],
              "context_line": "'a long string of contexta long string of contexta long string of contexta long string of contexta long string of contexta long string of contexta long string of",
              "post_context": []
            }
          ]
        }
      }
    ]
  },
  "breadcrumbs": {
    "values": [
      {
        "data": {
          "url": "https://pickaverb.webapp.com/api/something",
          "method": "POST",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644413370.714
      },
      {
        "data": {
          "url": "https://pickaverb.webapp.com/api/something",
          "method": "POST",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644413370.767
      },
      {
        "data": {
          "url": "https://pickaverb.webapp.com/api/something",
          "method": "POST",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644413371.123
      },
      {
        "data": {
          "url": "https://api-prod.webapp.com/api/v2/pages/detail_by_path/?queryofsomesort=what",
          "method": "GET",
          "status_code": 404
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644413382.215
      },
      {
        "data": {
          "url": "https://api-prod.webapp.com/api/redirects/?old_path=/?queryofsomesort=what",
          "method": "GET",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644413382.292
      },
      {
        "data": {
          "url": "https://api-prod.webapp.com/api/some-stuff/",
          "method": "GET",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644413382.476
      },
      {
        "data": {
          "url": "https://api-prod.webapp.com/api/settings/?queryofsomesort=what",
          "method": "GET",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644413382.508
      },
      {
        "data": {
          "url": "https://api-prod.webapp.com/api/more_stuff/",
          "method": "GET",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644413382.652
      },
      {
        "data": {
          "url": "https://api-prod.webapp.com/api/some_stuff/",
          "method": "GET",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644413382.655
      },
      {
        "data": {
          "url": "https://pickaverb.webapp.com/api/something",
          "method": "POST",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644413382.977
      },
      {
        "data": {
          "url": "https://api-prod.webapp.com/api/v2/pages/some_stuff/?queryofsomesort=what",
          "method": "GET",
          "status_code": 404
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644413388.568
      },
      {
        "data": {
          "url": "https://pickaverb.webapp.com/api/something",
          "method": "POST",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644413388.594
      },
      {
        "data": {
          "url": "https://pickaverb.webapp.com/api/something",
          "method": "POST",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644413388.602
      },
      {
        "data": {
          "url": "https://pickaverb.webapp.com/api/something",
          "method": "POST",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644413388.603
      },
      {
        "data": {
          "url": "https://pickaverb.webapp.com/api/something",
          "method": "POST",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644413388.632
      },
      {
        "data": {
          "url": "https://api-prod.webapp.com/api/redirects/?old_path=/?queryofsomesort=what",
          "method": "GET",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644413388.657
      },
      {
        "data": {
          "url": "https://api-prod.webapp.com/api/some_stuff/",
          "method": "GET",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644413388.859
      },
      {
        "data": {
          "url": "https://api-prod.webapp.com/api/settings/?queryofsomesort=what",
          "method": "GET",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644413388.862
      },
      {
        "data": {
          "url": "https://api-prod.webapp.com/api/more_stuff/",
          "method": "GET",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644413389.044
      },
      {
        "data": {
          "url": "https://api-prod.webapp.com/api/more_stuff/",
          "method": "GET",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644413389.046
      },
      {
        "data": {
          "url": "https://pickaverb.webapp.com/api/something",
          "method": "POST",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644413389.299
      },
      {
        "data": {
          "url": "https://api-prod.webapp.com/api/v2/pages/detail_by_path/?queryofsomesort=what",
          "method": "GET",
          "status_code": 404
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644413394.691
      },
      {
        "data": {
          "url": "https://pickaverb.webapp.com/api/something",
          "method": "POST",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644413394.711
      },
      {
        "data": {
          "url": "https://pickaverb.webapp.com/api/something",
          "method": "POST",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644413394.714
      },
      {
        "data": {
          "url": "https://pickaverb.webapp.com/api/something",
          "method": "POST",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644413394.722
      },
      {
        "data": {
          "url": "https://pickaverb.webapp.com/api/something",
          "method": "POST",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644413394.733
      },
      {
        "data": {
          "url": "https://api-prod.webapp.com/api/redirects/?old_path=/?queryofsomesort=what",
          "method": "GET",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644413394.771
      },
      {
        "data": {
          "url": "https://api-prod.webapp.com/api/some_stuff/",
          "method": "GET",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644413394.875
      },
      {
        "data": {
          "url": "https://api-prod.webapp.com/api/some_stuff/",
          "method": "GET",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644413394.876
      },
      {
        "data": {
          "url": "https://api-prod.webapp.com/api/settings/?queryofsomesort=what",
          "method": "GET",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644413394.877
      },
      {
        "data": {
          "url": "https://api-prod.webapp.com/api/some_stuff/",
          "method": "GET",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644413395.134
      },
      {
        "data": {
          "url": "https://pickaverb.webapp.com/api/something",
          "method": "POST",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644413395.156
      },
      {
        "data": {
          "url": "https://pickaverb.webapp.com/api/something",
          "method": "POST",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644444400.48
      },
      {
        "data": {
          "url": "https://pickaverb.webapp.com/api/something",
          "method": "POST",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644444400.5
      },
      {
        "data": {
          "url": "https://pickaverb.webapp.com/api/something",
          "method": "POST",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644444400.502
      },
      {
        "data": {
          "url": "https://pickaverb.webapp.com/api/something",
          "method": "POST",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644444400.545
      },
      {
        "data": {
          "url": "https://api-prod.webapp.com/api/v2/pages/detail_by_path/?queryofsomesort=what",
          "method": "GET",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644444400.977
      },
      {
        "data": {
          "url": "https://api-prod.webapp.com/api/settings/?queryofsomesort=what",
          "method": "GET",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644444401.22
      },
      {
        "data": {
          "url": "https://api-prod.webapp.com/api/some_stuff/",
          "method": "GET",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644444401.221
      },
      {
        "data": {
          "url": "https://api-prod.webapp.com/api/some_stuff/",
          "method": "GET",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644444401.231
      },
      {
        "data": {
          "url": "https://api-prod.webapp.com/api/some_stuff/",
          "method": "GET",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644444401.438
      },
      {
        "data": {
          "url": "https://pickaverb.webapp.com/api/something",
          "method": "POST",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644444402.527
      },
      {
        "data": {
          "url": "https://api-prod.webapp.com/api/v2/pages/detail_by_path/?queryofsomesort=what",
          "method": "GET",
          "status_code": 404
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644444411.162
      },
      {
        "data": {
          "url": "https://api-prod.webapp.com/api/redirects/?old_path=/us/some_stuff/stylers/?queryofsomesort=what",
          "method": "GET",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644444411.239
      },
      {
        "data": {
          "url": "https://pickaverb.webapp.com/api/something",
          "method": "POST",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644444411.409
      },
      {
        "data": {
          "url": "https://pickaverb.webapp.com/api/something",
          "method": "POST",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644444411.412
      },
      {
        "data": {
          "url": "https://pickaverb.webapp.com/api/something",
          "method": "POST",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644444411.418
      },
      {
        "data": {
          "url": "https://api-prod.webapp.com/api/settings/?queryofsomesort=what",
          "method": "GET",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644444411.425
      },
      {
        "data": {
          "url": "https://api-prod.webapp.com/api/some_stuff/",
          "method": "GET",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644444411.553
      },
      {
        "data": {
          "url": "https://pickaverb.webapp.com/api/something",
          "method": "POST",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644444411.555
      },
      {
        "data": {
          "url": "https://api-prod.webapp.com/api/some_stuff/",
          "method": "GET",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644444411.559
      },
      {
        "data": {
          "url": "https://api-prod.webapp.com/api/some_stuff/",
          "method": "GET",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644444411.563
      },
      {
        "type": "default",
        "level": "error",
        "message": "TypeError: Cannot read property 'id' of undefined\n    at /usr/src/app/dist/webapp-frontend/server/535.js:1:4444\n    at Array.find (<anonymous>)\n    at /usr/src/app/dist/webapp-frontend/server/535.js:1:4444\n    at /usr/src/app/dist/webapp-frontend/server/main.js:209:444455\n    at memoized (/usr/src/app/dist/webapp-frontend/server/main.js:209:444456)\n    at defaultStateFn (/usr/src/app/dist/webapp-frontend/server/main.js:209:212009)\n    at /usr/src/app/dist/webapp-frontend/server/main.js:209:212679\n    at memoized (/usr/src/app/dist/webapp-frontend/server/main.js:209:444456)\n    at MapSubscriber.project (/usr/src/app/dist/webapp-frontend/server/main.js:209:444456)\n    at MapSubscriber._next (/usr/src/app/dist/webapp-frontend/server/main.js:209:4444567)",
        "category": "console",
        "timestamp": 1644444411.651
      },
      {
        "type": "default",
        "level": "error",
        "message": "TypeError: Cannot read property 'id' of undefined\n    at /usr/src/app/dist/webapp-frontend/server/535.js:1:4444\n    at Array.find (<anonymous>)\n    at /usr/src/app/dist/webapp-frontend/server/535.js:1:4444\n    at /usr/src/app/dist/webapp-frontend/server/main.js:209:444455\n    at memoized (/usr/src/app/dist/webapp-frontend/server/main.js:209:444456)\n    at defaultStateFn (/usr/src/app/dist/webapp-frontend/server/main.js:209:212009)\n    at /usr/src/app/dist/webapp-frontend/server/main.js:209:212679\n    at memoized (/usr/src/app/dist/webapp-frontend/server/main.js:209:444456)\n    at MapSubscriber.project (/usr/src/app/dist/webapp-frontend/server/main.js:209:444456)\n    at MapSubscriber._next (/usr/src/app/dist/webapp-frontend/server/main.js:209:4444567)",
        "category": "console",
        "timestamp": 1644444411.663
      },
      {
        "type": "default",
        "level": "error",
        "message": "TypeError: Cannot read property 'id' of undefined\n    at /usr/src/app/dist/webapp-frontend/server/535.js:1:4444\n    at Array.find (<anonymous>)\n    at /usr/src/app/dist/webapp-frontend/server/535.js:1:4444\n    at /usr/src/app/dist/webapp-frontend/server/main.js:209:444455\n    at memoized (/usr/src/app/dist/webapp-frontend/server/main.js:209:444456)\n    at defaultStateFn (/usr/src/app/dist/webapp-frontend/server/main.js:209:212009)\n    at /usr/src/app/dist/webapp-frontend/server/main.js:209:212679\n    at memoized (/usr/src/app/dist/webapp-frontend/server/main.js:209:444456)\n    at MapSubscriber.project (/usr/src/app/dist/webapp-frontend/server/main.js:209:444456)\n    at MapSubscriber._next (/usr/src/app/dist/webapp-frontend/server/main.js:209:4444567)",
        "category": "console",
        "timestamp": 1644444411.708
      },
      {
        "type": "default",
        "level": "error",
        "message": "TypeError: Cannot read property 'id' of undefined\n    at /usr/src/app/dist/webapp-frontend/server/535.js:1:4444\n    at Array.find (<anonymous>)\n    at /usr/src/app/dist/webapp-frontend/server/535.js:1:4444\n    at /usr/src/app/dist/webapp-frontend/server/main.js:209:444455\n    at memoized (/usr/src/app/dist/webapp-frontend/server/main.js:209:444456)\n    at defaultStateFn (/usr/src/app/dist/webapp-frontend/server/main.js:209:212009)\n    at /usr/src/app/dist/webapp-frontend/server/main.js:209:212679\n    at memoized (/usr/src/app/dist/webapp-frontend/server/main.js:209:444456)\n    at MapSubscriber.project (/usr/src/app/dist/webapp-frontend/server/main.js:209:444456)\n    at MapSubscriber._next (/usr/src/app/dist/webapp-frontend/server/main.js:209:4444567)",
        "category": "console",
        "timestamp": 1644444411.719
      },
      {
        "type": "default",
        "level": "error",
        "message": "TypeError: Cannot read property 'id' of undefined\n    at /usr/src/app/dist/webapp-frontend/server/535.js:1:4444\n    at Array.find (<anonymous>)\n    at /usr/src/app/dist/webapp-frontend/server/535.js:1:4444\n    at /usr/src/app/dist/webapp-frontend/server/main.js:209:444455\n    at memoized (/usr/src/app/dist/webapp-frontend/server/main.js:209:444456)\n    at defaultStateFn (/usr/src/app/dist/webapp-frontend/server/main.js:209:212009)\n    at /usr/src/app/dist/webapp-frontend/server/main.js:209:212679\n    at memoized (/usr/src/app/dist/webapp-frontend/server/main.js:209:444456)\n    at MapSubscriber.project (/usr/src/app/dist/webapp-frontend/server/main.js:209:444456)\n    at MapSubscriber._next (/usr/src/app/dist/webapp-frontend/server/main.js:209:4444567)",
        "category": "console",
        "timestamp": 1644444411.73
      },
      {
        "type": "default",
        "level": "error",
        "message": "TypeError: Cannot read property 'id' of undefined\n    at /usr/src/app/dist/webapp-frontend/server/535.js:1:4444\n    at Array.find (<anonymous>)\n    at /usr/src/app/dist/webapp-frontend/server/535.js:1:4444\n    at /usr/src/app/dist/webapp-frontend/server/main.js:209:444455\n    at memoized (/usr/src/app/dist/webapp-frontend/server/main.js:209:444456)\n    at defaultStateFn (/usr/src/app/dist/webapp-frontend/server/main.js:209:212009)\n    at /usr/src/app/dist/webapp-frontend/server/main.js:209:212679\n    at memoized (/usr/src/app/dist/webapp-frontend/server/main.js:209:444456)\n    at /usr/src/app/dist/webapp-frontend/server/main.js:209:211963\n    at Array.map (<anonymous>)",
        "category": "console",
        "timestamp": 1644444411.741
      },
      {
        "data": {
          "url": "https://pickaverb.webapp.com/api/something",
          "method": "POST",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644444411.774
      },
      {
        "data": {
          "url": "https://pickaverb.webapp.com/api/something",
          "method": "POST",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644444417.218
      },
      {
        "data": {
          "url": "https://pickaverb.webapp.com/api/something",
          "method": "POST",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644444417.253
      },
      {
        "data": {
          "url": "https://pickaverb.webapp.com/api/something",
          "method": "POST",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644444417.338
      },
      {
        "data": {
          "url": "https://pickaverb.webapp.com/api/something",
          "method": "POST",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644444417.339
      },
      {
        "data": {
          "url": "https://api-prod.webapp.com/api/v2/pages/detail_by_path/?queryofsomesort=what",
          "method": "GET",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644444423.452
      },
      {
        "data": {
          "url": "https://api-prod.webapp.com/api/settings/?queryofsomesort=what",
          "method": "GET",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644444423.639
      },
      {
        "data": {
          "url": "https://api-prod.webapp.com/api/some_stuff/",
          "method": "GET",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644444423.74
      },
      {
        "data": {
          "url": "https://api-prod.webapp.com/api/some_stuff/",
          "method": "GET",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644444424.016
      },
      {
        "data": {
          "url": "https://api-prod.webapp.com/api/some_stuff/",
          "method": "GET",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644444424.019
      },
      {
        "data": {
          "url": "https://pickaverb.webapp.com/api/something",
          "method": "POST",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644444424.56
      },
      {
        "data": {
          "url": "https://pickaverb.webapp.com/api/something",
          "method": "POST",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644444431.249
      },
      {
        "data": {
          "url": "https://pickaverb.webapp.com/api/something",
          "method": "POST",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644444431.282
      },
      {
        "data": {
          "url": "https://pickaverb.webapp.com/api/something",
          "method": "POST",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644444431.301
      },
      {
        "data": {
          "url": "https://pickaverb.webapp.com/api/something",
          "method": "POST",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644444431.306
      },
      {
        "data": {
          "url": "https://api-prod.webapp.com/api/v2/pages/detail_by_path/?queryofsomesort=what",
          "method": "GET",
          "status_code": 404
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644444435.072
      },
      {
        "data": {
          "url": "https://api-prod.webapp.com/api/redirects/?old_path=/?queryofsomesort=what",
          "method": "GET",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644444435.101
      },
      {
        "data": {
          "url": "https://api-prod.webapp.com/api/some_stuff/",
          "method": "GET",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644444435.263
      },
      {
        "data": {
          "url": "https://api-prod.webapp.com/api/settings/?queryofsomesort=what",
          "method": "GET",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644444435.265
      },
      {
        "data": {
          "url": "https://api-prod.webapp.com/api/some_stuff/",
          "method": "GET",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644444435.425
      },
      {
        "data": {
          "url": "https://api-prod.webapp.com/api/some_stuff/",
          "method": "GET",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644444435.427
      },
      {
        "data": {
          "url": "https://pickaverb.webapp.com/api/something",
          "method": "POST",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644444435.609
      },
      {
        "data": {
          "url": "https://api-prod.webapp.com/api/v2/pages/detail_by_path/?queryofsomesort=what",
          "method": "GET",
          "status_code": 404
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644444441.261
      },
      {
        "data": {
          "url": "https://api-prod.webapp.com/api/redirects/?old_path=/us/some_stuff/stylers/?queryofsomesort=what",
          "method": "GET",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644444441.314
      },
      {
        "data": {
          "url": "https://pickaverb.webapp.com/api/something",
          "method": "POST",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644444441.416
      },
      {
        "data": {
          "url": "https://api-prod.webapp.com/api/v2/pages/detail_by_path/?queryofsomesort=what",
          "method": "GET",
          "status_code": 404
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644444441.418
      },
      {
        "data": {
          "url": "https://api-prod.webapp.com/api/v2/pages/detail_by_path/?queryofsomesort=what",
          "method": "GET",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644444441.421
      },
      {
        "data": {
          "url": "https://pickaverb.webapp.com/api/something",
          "method": "POST",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644444441.642
      },
      {
        "data": {
          "url": "https://pickaverb.webapp.com/api/something",
          "method": "POST",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644444441.643
      },
      {
        "data": {
          "url": "https://pickaverb.webapp.com/api/something",
          "method": "POST",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644444441.644
      },
      {
        "data": {
          "url": "https://api-prod.webapp.com/api/v2/pages/detail_by_path/?queryofsomesort=what",
          "method": "GET",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644444441.647
      },
      {
        "data": {
          "url": "https://api-prod.webapp.com/api/settings/?queryofsomesort=what",
          "method": "GET",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644444441.911
      },
      {
        "data": {
          "url": "https://api-prod.webapp.com/api/some_stuff/",
          "method": "GET",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644444442.028
      },
      {
        "data": {
          "url": "https://api-prod.webapp.com/api/some_stuff/",
          "method": "GET",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644444442.03
      },
      {
        "data": {
          "url": "https://api-prod.webapp.com/api/more_stuff/",
          "method": "GET",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644444442.053
      },
      {
        "data": {
          "url": "https://api-prod.webapp.com/api/redirects/?old_path=/?queryofsomesort=what",
          "method": "GET",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644444442.055
      },
      {
        "data": {
          "url": "https://api-prod.webapp.com/api/settings/?queryofsomesort=what",
          "method": "GET",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644444442.133
      },
      {
        "data": {
          "url": "https://api-prod.webapp.com/api/some_stuff/",
          "method": "GET",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644444442.25
      },
      {
        "data": {
          "url": "https://api-prod.webapp.com/api/some_stuff/",
          "method": "GET",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644444442.252
      },
      {
        "data": {
          "url": "https://api-prod.webapp.com/api/some_stuff/",
          "method": "GET",
          "status_code": 200
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644444442.522
      },
      {
        "data": {
          "url": "https://api-prod.webapp.com/api/yotpo/?queryofsomesort=what",
          "method": "GET",
          "status_code": "[undefined]"
        },
        "type": "http",
        "level": "info",
        "category": "http",
        "timestamp": 1644444442.538
      },
      {
        "type": "default",
        "level": "error",
        "message": "TypeError: Cannot read property 'id' of undefined\n    at /usr/src/app/dist/webapp-frontend/server/535.js:1:4444\n    at Array.find (<anonymous>)\n    at /usr/src/app/dist/webapp-frontend/server/535.js:1:4444\n    at /usr/src/app/dist/webapp-frontend/server/main.js:209:444455\n    at memoized (/usr/src/app/dist/webapp-frontend/server/main.js:209:444456)\n    at defaultStateFn (/usr/src/app/dist/webapp-frontend/server/main.js:209:212009)\n    at /usr/src/app/dist/webapp-frontend/server/main.js:209:212679\n    at memoized (/usr/src/app/dist/webapp-frontend/server/main.js:209:444456)\n    at MapSubscriber.project (/usr/src/app/dist/webapp-frontend/server/main.js:209:444456)\n    at MapSubscriber._next (/usr/src/app/dist/webapp-frontend/server/main.js:209:4444567)",
        "category": "console",
        "timestamp": 1644444442.623
      }
    ]
  },
  "environment": "production",
  "event_id": "ec321233a65d497e96edf4218f297efe",
  "level": "error",
  "datetime": "2022-02-09T13:30:42.633000Z"
}
