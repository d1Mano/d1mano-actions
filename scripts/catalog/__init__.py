"""The catalog agent: supplier chats in, proposed catalog changes out.

One run is a chain of short jobs. Each job is three processes, and which one you are in
decides what may exist in your process:

``run prepare``  holds the database key and no model. Claims the slice, downloads inputs.
``run read``     holds the model and no key. The only process that touches supplier text.
``run write``    holds the key and no model. Validates what ``read`` handed over, then
                 persists, confronts against the catalog, parks for approval or applies.

The modules, by the half they belong to:

* no key, no socket -- ``sources`` (find the chat in an upload), ``whatsapp`` and ``dates``
  (separate and date the products), ``ordinals`` (stable identities), ``ingest`` (step 1),
  ``compare`` (step 2: one list out of several suppliers'), ``triage`` (ask the model about
  the pairs code could not decide), ``dbface`` (step 3: verdicts against a catalog somebody
  else read), ``proposal`` (the exact write a verdict implies), ``validate`` (the gate);
* the key -- ``postgrest`` (the one door to the database and to private storage) and
  ``apply`` (step 5: gate, write, stamp, close).

Five rules span the package:

* a name that came out of an archive is never used to build a path;
* a value that came out of a chat is never used to build SQL or a shell command, and the
  client that would have to accept one does not exist;
* the model proposes and code disposes: an answer is used only after two independent
  validations, and it can never undo a separation code already made;
* nothing iterates a ``set`` to number things. Ordinals are the identity rows are keyed by,
  and replay safety rides on them, so every ordering is an explicit sort on a total key;
* every product that enters leaves with a recorded outcome. If the arithmetic does not
  close, the stage raises instead of writing a shorter list.

This repository is public. Nothing here names the business, its suppliers, its products or
its schema beyond the tables it writes; rules, prompt and data arrive at run time.
"""

__all__ = [
    "apply",
    "compare",
    "dates",
    "dbface",
    "ingest",
    "limits",
    "logmask",
    "ordinals",
    "postgrest",
    "proposal",
    "run",
    "sources",
    "triage",
    "validate",
    "whatsapp",
]
