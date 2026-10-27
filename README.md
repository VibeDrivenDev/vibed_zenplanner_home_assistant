# ZenPlanner

ZenPlanner puts the classes you hold on a Home Assistant calendar. Open classes for the next 14 days show on a sensor. You reserve and cancel from a service call.

## Add the integration

1. In HACS, open Custom repositories.
2. Add this repository as an integration.
3. Install ZenPlanner and restart Home Assistant.

## Sign in

Open Settings, then Devices and services, then Add integration, then ZenPlanner.

Enter the username and password from the ZenPlanner member app.

When the account belongs to more than one gym, pick one gym. A second gym is a second config entry.

When the family has more than one person, choose who gets a calendar and a sensor. The person who signed in starts selected. A one-person family skips that step.

## Read the schedule

Each selected person has a calendar and an upcoming sensor on the gym device.

The calendar lists sessions that person holds. A hold is a reserved seat, a waitlisted seat, or a hold the app cannot cancel. Open classes stay off the calendar, so Home Assistant does not mark you busy for a class you did not take.

The sensor state is the name of the next open class in the next 14 days, or `none`. The `classes` attribute lists up to 10 of those open classes. The `count` attribute is the full number. Copy `class_id` from `classes`.

Call `zenplanner.list_classes` on the person's calendar or upcoming sensor to get every class stored in that 14 day horizon, held and open.

To refresh the horizon, use Home Assistant's update entity action on the calendar. Reserve and cancel reload before they decide.

## Reserve a class

```yaml
service: zenplanner.reserve
target:
  entity_id: calendar.ada_lovelace
data:
  class_id: "0f3c1b2e-6a7d-4e18-9c0a-11b2d3445501"
  join_waitlist: false
```

`join_waitlist` defaults to false. A full class returns `spots_full` and stays open. Set `join_waitlist` to true to join the waitlist after that class is full.

`result` is `reserved`, `waitlisted`, `held`, `spots_full`, `waitlist_full`, or `not_found`. A full class is a normal response. The response has no membership id and no attendance id.

Reserve again for a class this person already holds and the result is the seat they already have. The integration does not book a second seat.

## Cancel a class

```yaml
service: zenplanner.cancel
target:
  entity_id: calendar.ada_lovelace
data:
  class_id: "0f3c1b2e-6a7d-4e18-9c0a-11b2d3445501"
```

A cancel that drops the seat returns `{"result": "cleared"}`. A cancel that finds no held seat returns `{"result": "already_clear"}` and does not send another delete. A hold with no cancel id fails validation. If ZenPlanner still shows the seat after the delete, the call fails.

## Change who has entities

Open the integration options and select people. The list is the current family. Turning a person off removes that person's calendar and sensor.

## Sign in again

When the password changes, Home Assistant asks for the password only. The gym stays the same.

## Limits

This integration does not collect a card. It does not buy workshops, drop-ins, or appointments, and it does not check you in.
