# The pinned Rust binding exposes whole-transaction verification. Add only the
# C calling convention for the existing BDK single-input VerifySpend method.
if(CMAKE_SOURCE_DIR STREQUAL PROJECT_SOURCE_DIR AND PROJECT_NAME STREQUAL "bdk")
    function(scary_bind_witnessed_spend)
        target_sources(static-bdkffi PRIVATE "${SCARY_NATIVE_DIR}/verify_spend.cpp")
    endfunction()
    cmake_language(DEFER CALL scary_bind_witnessed_spend)
endif()
