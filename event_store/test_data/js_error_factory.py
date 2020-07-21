throw_error = {
    "exception": {
        "values": [
            {
                "type": "Error",
                "value": "an error string",
                "stacktrace": {
                    "frames": [
                        {
                            "colno": 27,
                            "filename": "http://localhost:4200/polyfills.js",
                            "function": "globalZoneAwareCallback",
                            "in_app": True,
                            "lineno": 4864,
                        },
                        {
                            "colno": 14,
                            "filename": "http://localhost:4200/polyfills.js",
                            "function": "invokeTask",
                            "in_app": True,
                            "lineno": 4838,
                        },
                        {
                            "colno": 34,
                            "filename": "http://localhost:4200/polyfills.js",
                            "function": "invokeTask",
                            "in_app": True,
                            "lineno": 3700,
                        },
                        {
                            "colno": 47,
                            "filename": "http://localhost:4200/polyfills.js",
                            "function": "runTask",
                            "in_app": True,
                            "lineno": 3403,
                        },
                        {
                            "colno": 60,
                            "filename": "http://localhost:4200/polyfills.js",
                            "function": "invokeTask",
                            "in_app": True,
                            "lineno": 3625,
                        },
                        {
                            "colno": 33,
                            "filename": "http://localhost:4200/vendor.js",
                            "function": "onInvokeTask",
                            "in_app": True,
                            "lineno": 70625,
                        },
                        {
                            "colno": 31,
                            "filename": "http://localhost:4200/polyfills.js",
                            "function": "invokeTask",
                            "in_app": True,
                            "lineno": 3626,
                        },
                        {
                            "colno": 23,
                            "filename": "http://localhost:4200/vendor.js",
                            "function": "sentryWrapped",
                            "in_app": True,
                            "lineno": 81826,
                        },
                        {
                            "colno": 50,
                            "filename": "http://localhost:4200/vendor.js",
                            "function": "decoratePreventDefault/<",
                            "in_app": True,
                            "lineno": 79300,
                        },
                        {
                            "colno": 29,
                            "filename": "http://localhost:4200/vendor.js",
                            "function": "renderEventHandlerClosure/<",
                            "in_app": True,
                            "lineno": 73554,
                        },
                        {
                            "colno": 25,
                            "filename": "http://localhost:4200/vendor.js",
                            "function": "dispatchEvent",
                            "in_app": True,
                            "lineno": 61709,
                        },
                        {
                            "colno": 12,
                            "filename": "http://localhost:4200/vendor.js",
                            "function": "debugHandleEvent",
                            "in_app": True,
                            "lineno": 75876,
                        },
                        {
                            "colno": 15,
                            "filename": "http://localhost:4200/vendor.js",
                            "function": "callWithDebugContext",
                            "in_app": True,
                            "lineno": 76251,
                        },
                        {
                            "colno": 15,
                            "filename": "http://localhost:4200/vendor.js",
                            "function": "viewWrappedDebugError",
                            "in_app": True,
                            "lineno": 61054,
                        },
                    ]
                },
                "mechanism": {"handled": True, "type": "generic"},
            }
        ]
    },
    "level": "error",
    "event_id": "3a4fe46760d04d0c9eeb4f2be32d2ba2",
    "platform": "javascript",
    "sdk": {
        "name": "sentry.javascript.browser",
        "packages": [{"name": "npm:@sentry/browser", "version": "5.11.0"}],
        "version": "5.11.0",
        "integrations": [
            "InboundFilters",
            "FunctionToString",
            "TryCatch",
            "Breadcrumbs",
            "GlobalHandlers",
            "LinkedErrors",
            "UserAgent",
        ],
    },
    "breadcrumbs": [
        {
            "timestamp": 1578861859.377,
            "category": "console",
            "data": {
                "extra": {
                    "arguments": [
                        "Angular is running in the development mode. Call enableProdMode() to enable the production mode."
                    ]
                },
                "logger": "console",
            },
            "level": "log",
            "message": "Angular is running in the development mode. Call enableProdMode() to enable the production mode.",
        },
        {
            "timestamp": 1578861859.415,
            "category": "ui.click",
            "message": "body > app-root > ol > li > a",
        },
        {
            "timestamp": 1578861859.529,
            "category": "xhr",
            "data": {
                "method": "GET",
                "url": "http://localhost:4200/sockjs-node/info?t=1578861859416",
                "status_code": 200,
            },
            "type": "http",
        },
        {
            "timestamp": 1578861860.967,
            "category": "ui.click",
            "message": "body > app-root > ol > li > a",
        },
        {
            "timestamp": 1578861861.053,
            "category": "sentry",
            "event_id": "953c99e1547f4f6bbcb119dc44cc7d34",
            "level": "error",
            "message": "Error: an error string",
        },
        {
            "timestamp": 1578861861.071,
            "category": "sentry",
            "event_id": "1b480b60cbf840ad9a87c6c8d32eaca4",
            "level": "error",
            "message": "Error: an error string",
        },
        {
            "timestamp": 1578861861.072,
            "category": "sentry",
            "event_id": "21ea06f91e1b429b8a6e021be50c7104",
            "level": "error",
            "message": "Error: an error string",
        },
        {
            "timestamp": 1578861861.075,
            "category": "sentry",
            "event_id": "7de9ecf53d7f480c86a857acdd328f3c",
            "level": "error",
            "message": "Error: an error string",
        },
        {
            "timestamp": 1578862058.96,
            "category": "ui.click",
            "message": "body > app-root > ol > li > a",
        },
        {
            "timestamp": 1578862059.054,
            "category": "sentry",
            "event_id": "15f869407e7049d391a74bc26b98fb0e",
            "level": "error",
            "message": "Error: an error string",
        },
        {
            "timestamp": 1578862059.176,
            "category": "sentry",
            "event_id": "eb0ae9354bdb4ecd90614de54495549b",
            "level": "error",
            "message": "Error: an error string",
        },
        {
            "timestamp": 1578862349.058,
            "category": "ui.click",
            "message": "body > app-root > ol > li > a",
        },
        {
            "timestamp": 1578862349.163,
            "category": "sentry",
            "event_id": "ff7ff13b69ee46789121fd071e8bfe70",
            "level": "error",
            "message": "Error: an error string",
        },
        {
            "timestamp": 1578862349.226,
            "category": "sentry",
            "event_id": "fa8e4ca917864dd7980fc1dd020783de",
            "level": "error",
            "message": "Error: an error string",
        },
        {
            "timestamp": 1578862369.686,
            "category": "ui.click",
            "message": "body > app-root > ol > li > a",
        },
        {
            "timestamp": 1578862369.811,
            "category": "sentry",
            "event_id": "2cec68bc7a484f64980987fb53471a8c",
            "level": "error",
            "message": "Error: an error string",
        },
        {
            "timestamp": 1578862369.812,
            "category": "sentry",
            "event_id": "72ccc32c539e407fa4fab5737df31dbf",
            "level": "error",
            "message": "Error: an error string",
        },
        {
            "timestamp": 1578862392.876,
            "category": "ui.click",
            "message": "body > app-root > ol > li > a",
        },
        {
            "timestamp": 1578862393.05,
            "category": "sentry",
            "event_id": "abed54c077f24206aec40a04f4a838a6",
            "level": "error",
            "message": "Error: an error string",
        },
        {
            "timestamp": 1578862393.053,
            "category": "sentry",
            "event_id": "abf87281aee443fca8169590ba6389bb",
            "level": "error",
            "message": "Error: an error string",
        },
        {
            "timestamp": 1578862442.414,
            "category": "ui.click",
            "message": "body > app-root > ol > li > a",
        },
        {
            "timestamp": 1578862442.473,
            "category": "sentry",
            "event_id": "589f50e20cec479fac95415dbe021cb7",
            "level": "error",
            "message": "Error: an error string",
        },
        {
            "timestamp": 1578862442.522,
            "category": "sentry",
            "event_id": "8d48047f80c645868a90f72e0c3f6487",
            "level": "error",
            "message": "Error: an error string",
        },
        {
            "timestamp": 1578862473.404,
            "category": "ui.click",
            "message": "body > app-root > ol > li > a",
        },
        {
            "timestamp": 1578862473.554,
            "category": "sentry",
            "event_id": "ee14e02ac9754f79b827505548d36c47",
            "level": "error",
            "message": "Error: an error string",
        },
        {
            "timestamp": 1578862473.556,
            "category": "sentry",
            "event_id": "12d83a79d84b40f1a2df863d1af657d1",
            "level": "error",
            "message": "Error: an error string",
        },
        {
            "timestamp": 1578862870.517,
            "category": "ui.click",
            "message": "body > app-root > ol > li > a",
        },
        {
            "timestamp": 1578862870.698,
            "category": "sentry",
            "event_id": "692223e6f4384e60b53bb19d0adde8b9",
            "level": "error",
            "message": "Error: an error string",
        },
        {
            "timestamp": 1578862870.706,
            "category": "sentry",
            "event_id": "a7de5cc48c2544d89e43f2795e0e5457",
            "level": "error",
            "message": "Error: an error string",
        },
        {
            "timestamp": 1578862876.059,
            "category": "ui.click",
            "message": "body > app-root > ol > li > a",
        },
        {
            "timestamp": 1578862876.202,
            "category": "sentry",
            "event_id": "aa64fb0f82254c0b8982a21b948bf2d8",
            "level": "error",
            "message": "Error: an error string",
        },
        {
            "timestamp": 1578862876.204,
            "category": "sentry",
            "event_id": "7e590f61436447aebf2b7176023bab19",
            "level": "error",
            "message": "Error: an error string",
        },
    ],
    "request": {
        "url": "http://localhost:4200/",
        "headers": {
            "User-Agent": "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:72.0) Gecko/20100101 Firefox/72.0"
        },
    },
}


sentry_browser_js_data = {
  "exception": {
    "values": [
      {
        "type": "Error",
        "value": "A Generic Error 9a9132",
        "stacktrace": {
          "frames": [
            {
              "colno": 27,
              "filename": "http://localhost:4201/polyfills.js",
              "function": "globalZoneAwareCallback",
              "in_app": True,
              "lineno": 4864
            },
            {
              "colno": 14,
              "filename": "http://localhost:4201/polyfills.js",
              "function": "invokeTask",
              "in_app": True,
              "lineno": 4838
            },
            {
              "colno": 34,
              "filename": "http://localhost:4201/polyfills.js",
              "function": "invokeTask",
              "in_app": True,
              "lineno": 3700
            },
            {
              "colno": 47,
              "filename": "http://localhost:4201/polyfills.js",
              "function": "runTask",
              "in_app": True,
              "lineno": 3403
            },
            {
              "colno": 60,
              "filename": "http://localhost:4201/polyfills.js",
              "function": "invokeTask",
              "in_app": True,
              "lineno": 3625
            },
            {
              "colno": 33,
              "filename": "http://localhost:4201/vendor.js",
              "function": "onInvokeTask",
              "in_app": True,
              "lineno": 73280
            },
            {
              "colno": 31,
              "filename": "http://localhost:4201/polyfills.js",
              "function": "invokeTask",
              "in_app": True,
              "lineno": 3626
            },
            {
              "colno": 23,
              "filename": "http://localhost:4201/vendor.js",
              "function": "sentryWrapped",
              "in_app": True,
              "lineno": 84582
            },
            {
              "colno": 50,
              "filename": "http://localhost:4201/vendor.js",
              "function": "decoratePreventDefault/<",
              "in_app": True,
              "lineno": 81955
            },
            {
              "colno": 29,
              "filename": "http://localhost:4201/vendor.js",
              "function": "renderEventHandlerClosure/<",
              "in_app": True,
              "lineno": 76209
            },
            {
              "colno": 25,
              "filename": "http://localhost:4201/vendor.js",
              "function": "dispatchEvent",
              "in_app": True,
              "lineno": 64364
            },
            {
              "colno": 12,
              "filename": "http://localhost:4201/vendor.js",
              "function": "debugHandleEvent",
              "in_app": True,
              "lineno": 78531
            },
            {
              "colno": 15,
              "filename": "http://localhost:4201/vendor.js",
              "function": "callWithDebugContext",
              "in_app": True,
              "lineno": 78906
            },
            {
              "colno": 15,
              "filename": "http://localhost:4201/vendor.js",
              "function": "viewWrappedDebugError",
              "in_app": True,
              "lineno": 63709
            }
          ]
        },
        "mechanism": { "handled": True, "type": "generic" }
      }
    ]
  },
  "level": "error",
  "event_id": "70ecf4a90a2a4ce9bc0474b1d5db8460",
  "platform": "javascript",
  "sdk": {
    "name": "sentry.javascript.browser",
    "packages": [{ "name": "npm:@sentry/browser", "version": "5.19.2" }],
    "version": "5.19.2",
    "integrations": [
      "InboundFilters",
      "FunctionToString",
      "Breadcrumbs",
      "GlobalHandlers",
      "LinkedErrors",
      "UserAgent",
      "TryCatch"
    ]
  },
  "timestamp": 1595264892.911,
  "breadcrumbs": [
    {
      "timestamp": 1595264883.019,
      "category": "console",
      "data": {
        "arguments": [
          "Angular is running in the development mode. Call enableProdMode() to enable the production mode."
        ],
        "logger": "console"
      },
      "level": "log",
      "message": "Angular is running in the development mode. Call enableProdMode() to enable the production mode."
    },
    {
      "timestamp": 1595264883.103,
      "category": "ui.click",
      "message": "body > app-root > ol > li > button"
    },
    {
      "timestamp": 1595264883.231,
      "category": "xhr",
      "data": {
        "method": "GET",
        "url": "http://localhost:4201/sockjs-node/info?t=1595264883921",
        "status_code": 200
      },
      "type": "http"
    }
  ],
  "request": {
    "url": "http://localhost:4201/",
    "headers": {
      "User-Agent": "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:78.0) Gecko/20100101 Firefox/78.0"
    }
  }
}

sentry_browser_js_data_old = {
  "exception": {
    "values": [
      {
        "type": "Error",
        "value": "A Generic Error d98cef",
        "stacktrace": {
          "frames": [
            {
              "colno": 27,
              "filename": "http://localhost:4201/polyfills.js",
              "function": "globalZoneAwareCallback",
              "in_app": True,
              "lineno": 4864
            },
            {
              "colno": 14,
              "filename": "http://localhost:4201/polyfills.js",
              "function": "invokeTask",
              "in_app": True,
              "lineno": 4838
            },
            {
              "colno": 34,
              "filename": "http://localhost:4201/polyfills.js",
              "function": "invokeTask",
              "in_app": True,
              "lineno": 3700
            },
            {
              "colno": 47,
              "filename": "http://localhost:4201/polyfills.js",
              "function": "runTask",
              "in_app": True,
              "lineno": 3403
            },
            {
              "colno": 60,
              "filename": "http://localhost:4201/polyfills.js",
              "function": "invokeTask",
              "in_app": True,
              "lineno": 3625
            },
            {
              "colno": 33,
              "filename": "http://localhost:4201/vendor.js",
              "function": "onInvokeTask",
              "in_app": True,
              "lineno": 73280
            },
            {
              "colno": 31,
              "filename": "http://localhost:4201/polyfills.js",
              "function": "invokeTask",
              "in_app": True,
              "lineno": 3626
            },
            {
              "colno": 23,
              "filename": "http://localhost:4201/vendor.js",
              "function": "sentryWrapped",
              "in_app": True,
              "lineno": 84481
            },
            {
              "colno": 50,
              "filename": "http://localhost:4201/vendor.js",
              "function": "decoratePreventDefault/<",
              "in_app": True,
              "lineno": 81955
            },
            {
              "colno": 29,
              "filename": "http://localhost:4201/vendor.js",
              "function": "renderEventHandlerClosure/<",
              "in_app": True,
              "lineno": 76209
            },
            {
              "colno": 25,
              "filename": "http://localhost:4201/vendor.js",
              "function": "dispatchEvent",
              "in_app": True,
              "lineno": 64364
            },
            {
              "colno": 12,
              "filename": "http://localhost:4201/vendor.js",
              "function": "debugHandleEvent",
              "in_app": True,
              "lineno": 78531
            },
            {
              "colno": 15,
              "filename": "http://localhost:4201/vendor.js",
              "function": "callWithDebugContext",
              "in_app": True,
              "lineno": 78906
            },
            {
              "colno": 15,
              "filename": "http://localhost:4201/vendor.js",
              "function": "viewWrappedDebugError",
              "in_app": True,
              "lineno": 63709
            }
          ]
        },
        "mechanism": { "handled": True, "type": "generic" }
      }
    ]
  },
  "level": "error",
  "event_id": "e3c372019d644a44b8b2bd65f02e4196",
  "platform": "javascript",
  "sdk": {
    "name": "sentry.javascript.browser",
    "packages": [{ "name": "npm:@sentry/browser", "version": "5.11.0" }],
    "version": "5.11.0",
    "integrations": [
      "InboundFilters",
      "FunctionToString",
      "TryCatch",
      "Breadcrumbs",
      "GlobalHandlers",
      "LinkedErrors",
      "UserAgent"
    ]
  },
  "breadcrumbs": [
    {
      "timestamp": 1595265002.404,
      "category": "console",
      "data": {
        "extra": {
          "arguments": [
            "Angular is running in the development mode. Call enableProdMode() to enable the production mode."
          ]
        },
        "logger": "console"
      },
      "level": "log",
      "message": "Angular is running in the development mode. Call enableProdMode() to enable the production mode."
    },
    {
      "timestamp": 1595265002.501,
      "category": "ui.click",
      "message": "body > app-root > ol > li > button"
    },
    {
      "timestamp": 1595265002.557,
      "category": "xhr",
      "data": {
        "method": "GET",
        "url": "http://localhost:4201/sockjs-node/info?t=1595265002505",
        "status_code": 200
      },
      "type": "http"
    }
  ],
  "request": {
    "url": "http://localhost:4201/",
    "headers": {
      "User-Agent": "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:78.0) Gecko/20100101 Firefox/78.0"
    }
  }
}

