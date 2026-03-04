/* XXX_LICENSE_HEADER_START_XXX */
/****************License************************************************
 *
 * Copyright 2000-2001.  SpeechWorks International, Inc.  
 *
 * Use of this software is subject to notices and obligations set forth
 * in the SpeechWorks Public License - Software Version 1.1 which is
 * included with this software.
 *
 * SpeechWorks is a registered trademark, and SpeechWorks Here, 
 * DialogModules and the SpeechWorks logo are trademarks of SpeechWorks 
 * International, Inc. in the United States and other countries. 
 * 
 * $Id: VXIheaderSuffix.h,v 1.4.24.2 2003/11/11 15:15:36 mpanacci Exp $
 ************************************************************************/
/* XXX_LICENSE_HEADER_STOP_XXX */
/************************************************************************
 *
 *
 * Settings which should end all public headers
 *
 ************************************************************************
 */

/* Reset the structure packing conventions */

#if defined(_MSC_VER)            /* Microsoft Visual C++ */
  #pragma pack(pop)
#elif defined(__BORLANDC__)      /* Borland C++ */
  #pragma option -a.
#elif defined(__WATCOMC__)       /* Watcom C++ */
  #pragma pack(pop)
#endif
