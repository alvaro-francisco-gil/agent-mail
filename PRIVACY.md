# Privacy policy

This policy covers the read-only personal-data tools in this repository and the
applications registered to run them, including the LinkedIn application
**`alvaro-professional-record`** (client id `78sxk1a1k51xr8`), registered under
the EU Digital Markets Act to read its author's own LinkedIn data through the
Member Data Portability API.

**Last updated:** 2026-09-23

## Who this is for

One person: Álvaro Francisco Gil, the author. These tools read **his own data,
from accounts he owns, with his own authorisation.** There are no other users,
no sign-up, and no way for anyone else to authorise them.

## What is collected

Only the data of the account holder who authorises the application, and only the
categories that account holder selects at the time:

- From LinkedIn: his own posts, articles, comments and reposts, as returned by
  the Member Data Portability API.
- From a mailbox: message metadata and content, read on demand.

**No data about any other person is collected as a subject of this processing.**
Where his own data unavoidably names other people — a post that mentions someone,
an email from a correspondent — that content is handled under the rules in the
repositories that consume it, which distil facts rather than storing third-party
contact details.

## What happens to it

- Stored in **private repositories and on machines the author controls.**
- Never published, sold, rented, or shared with any third party.
- Never used to train a model, build a profile, or target advertising.
- No analytics, no telemetry, no tracking of any kind.

## Access tokens

Tokens are held in a password manager and read from the environment at run time.
They are never committed to a repository.

## Read-only by construction

These tools cannot write, send, delete or modify anything in the accounts they
read. That is enforced by the scopes the provider issues rather than by a promise
the code makes about itself — see
[the reasoning](https://github.com/alvaro-francisco-gil/agent-mail).

## Revoking access

The account holder can revoke authorisation at any time from the provider's own
settings — for LinkedIn, under data and permitted services. After revocation the
application can read nothing.

## Retention and deletion

Data is retained for as long as the author finds it useful and is deleted by
deleting the files. There is no server, no database and no backup held by anyone
else.

## Contact

alvaro.francisco.gil@gmail.com
