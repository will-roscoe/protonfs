Getting Started
===============

.. contents:: Contents
   :local:
   :depth: 1

Installation
------------

.. code-block:: bash

   pip install protonfs


Post Installation Setup
-----------------------
`protonfs` also requires the `proton-drive <https://proton.me/download/drive/cli/index.html>`_
CLI. The latest compatible version can be installed via 

.. code-block:: bash
   protonfs install-drive
   
Alternatively you can supply the binary yourself by passing it via `PROTONFS_DRIVE_BIN` env variable, or ensuring it is added to `PATH`.

You should then log in to proton drive if you have not already done so.
Follow the standard proton-drive auth instructions or login interactively using :code:`protonfs auth login`.

.. Quick Start
.. -----------


.. Configuration
.. -------------

