These events are taken from open source sentry to test for compatibility. `/api/0/issues/<issue_id>/events/latest/`

1. Make an event in a error factory project
2. Save the JSON in the test_data/incoming_events directory.
3. Send the same JSON to an open source sentry instance. Use something such as postwomen. Do not send it from the error factory project again as it will have a different timestamp and event id.
4. Save results in this directory.
