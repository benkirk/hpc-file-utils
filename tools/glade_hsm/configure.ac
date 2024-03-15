AC_INIT([glade_hsm], [1.0], [benkirk@ucar.edu])

AC_MSG_RESULT([---------------------------------------------])
AC_MSG_RESULT([-------- Configuring glade_hsm --------------])
AC_MSG_RESULT([---------------------------------------------])

AC_CONFIG_SRCDIR([glade_hsm])

top_srcdir=$(dirname $0)

AC_CONFIG_AUX_DIR([build-aux])
#AC_CONFIG_MACRO_DIR([m4])

# Print configuration info to the header file
#AC_DEFINE_UNQUOTED(CONFIGURE_INFO, ["$0 $ac_configure_args"], [Configuration information.])

AM_INIT_AUTOMAKE([dist-bzip2 tar-pax subdir-objects color-tests 1.11 foreign -Wall -Werror])
AM_SILENT_RULES(yes) dnl # use silent rules - automake 1.11

#AC_PROG_CC
#AC_CONFIG_HEADERS([config.h])
AC_CONFIG_FILES([
 Makefile
 tests/Makefile
])
AC_OUTPUT
