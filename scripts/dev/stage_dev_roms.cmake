# Copy the firmware image from the project's /roms directory into the data
# folder the standalone app reads at startup.
#
# Without this a locally built app shows "A Gearmulator MD firmware rom (8 MB
# .bin) is required, but was not found", because only `dev.py` stages firmware
# and it stages into an isolated throwaway root -- not the real data folder an
# app launched from Finder or an IDE uses.
#
# Invoked as a POST_BUILD step with:
#   -DROMS_DIR=  project /roms
#   -DDATA_DIR=  product data folder (…/<vendor>/<product>)
#   -DROM_SHA256=SHA-256 of the image this product accepts
#
# Missing firmware is not an error: /roms is gitignored, and a contributor
# without it must still be able to build.
#
# Identification is by content, not filename. The loader fingerprints the
# image it loads, so a renamed or swapped file must not install as the wrong
# product -- MD and MM would otherwise happily receive each other's firmware
# and fail later, at device boot, with a far less obvious message.

if(NOT IS_DIRECTORY "${ROMS_DIR}")
	return()
endif()

file(GLOB roms "${ROMS_DIR}/*.bin")

foreach(rom ${roms})
	file(SIZE "${rom}" size)
	if(NOT size EQUAL 8388608)
		continue()
	endif()

	file(SHA256 "${rom}" actual)
	if(NOT actual STREQUAL "${ROM_SHA256}")
		continue()
	endif()

	get_filename_component(name "${rom}" NAME)
	set(dest "${DATA_DIR}/roms/${name}")

	# Skip identical destinations so an incremental build does not rewrite
	# 8 MiB on every link.
	if(EXISTS "${dest}")
		file(SHA256 "${dest}" dest_hash)
		if(dest_hash STREQUAL actual)
			return()
		endif()
	endif()

	file(MAKE_DIRECTORY "${DATA_DIR}/roms")
	file(COPY "${rom}" DESTINATION "${DATA_DIR}/roms")
	message(STATUS "Staged firmware ${name} -> ${DATA_DIR}/roms")
	return()
endforeach()
